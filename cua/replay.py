"""Deterministic replay: the production execution path.

Given an artifact + typed parameters, execute the recorded flow with NO model
in the decision loop. All branching is data-driven from the artifact:

  per step:
    resolve target via the ranked strategy list (recording which strategy won)
    -> on failure, scan the page in a fixed order:
         1. hard-error detectors      -> stop with a debuggable failure
         2. outcome detectors         -> return an expected business outcome
         3. recovery rules            -> apply the mechanical fix, retry step
         4. (optionally) escalate to a human; resume = restart from step 1
    -> after success, scan hard errors + outcomes too (a page can answer the
       business question mid-flow, e.g. "No member found")

  after all steps:
    verify the checkpoint (never assume the last click worked), then extract
    declared outputs. Checkpoint miss re-scans outcomes first: a validation
    error page is a business outcome, not a crash.

Resume-after-escalation restarts the run from the beginning: capabilities are
non-mutating up to their recorded boundary (risk.stops_before), so restart is
safe and keeps the control-transfer model simple. See REPORT.md.
"""
from __future__ import annotations

import time
from typing import Optional

from .policy import Policy, PolicyViolation, Redactor
from .schema import (
    Artifact, Condition, EscalationReport, FailureDetail, OutcomeDetail,
    ReplayResult, ReplayStatus, Step, StepReport, utc_now,
)
from .surface import BrowserSurface, ResolveError
from .tracelog import RunLogger


class _BusinessOutcome(Exception):
    def __init__(self, detail: OutcomeDetail):
        self.detail = detail


class _HardFailure(Exception):
    def __init__(self, detail: FailureDetail):
        self.detail = detail


class _Restart(Exception):
    def __init__(self, report: EscalationReport):
        self.report = report


class _Abort(Exception):
    def __init__(self, report: EscalationReport):
        self.report = report


class ReplayEngine:
    def __init__(
        self,
        artifact: Artifact,
        params: dict[str, str],
        policy: Policy,
        surface: BrowserSurface,
        logger: RunLogger,
        intervention_manager=None,
        allow_draft: bool = False,
        max_restarts: int = 1,
    ):
        self.a = artifact
        self.params = artifact.validate_params(params)
        self.policy = policy
        self.surface = surface
        self.log = logger
        self.interventions = intervention_manager
        self.allow_draft = allow_draft
        self.max_restarts = max_restarts
        self._recovery_uses: dict[str, int] = {}

    # ------------------------------------------------------------------ run
    def run(self) -> ReplayResult:
        result = ReplayResult(
            run_id=self.log.run_id,
            capability_id=self.a.capability_id,
            artifact_version=self.a.version,
            status=ReplayStatus.HARD_FAILURE,
            started_at=utc_now(),
            evidence_dir=str(self.log.dir),
        )
        if self.a.status != "approved" and not self.allow_draft:
            result.failure = FailureDetail(
                expected="an approved artifact (or --allow-draft)",
                observed=f"artifact status is '{self.a.status}'",
            )
            result.finished_at = utc_now()
            return result

        restarts = 0
        while True:
            try:
                self._run_once(result)
                break
            except _BusinessOutcome as bo:
                result.status = ReplayStatus.BUSINESS_OUTCOME
                result.outcome = bo.detail
                result.outputs = bo.detail.outputs
                break
            except _HardFailure as hf:
                result.status = ReplayStatus.HARD_FAILURE
                result.failure = hf.detail
                break
            except _Restart as rs:
                result.escalations.append(rs.report)
                restarts += 1
                if restarts > self.max_restarts:
                    result.status = ReplayStatus.HARD_FAILURE
                    result.failure = FailureDetail(
                        expected="run to complete after human handoff",
                        observed=f"restart budget ({self.max_restarts}) exhausted",
                    )
                    break
                self.log.event("restart_after_handoff", attempt=restarts)
                result.steps.clear()
                self._recovery_uses.clear()
                continue
            except _Abort as ab:
                result.escalations.append(ab.report)
                result.status = ReplayStatus.ABORTED
                result.failure = FailureDetail(
                    step_id=ab.report.at_step,
                    expected="operator to resume the run",
                    observed=f"operator resolution: {ab.report.resolution}",
                )
                break
            except PolicyViolation as pv:
                result.status = ReplayStatus.HARD_FAILURE
                result.failure = FailureDetail(
                    expected="all actions inside the safety policy",
                    observed=str(pv),
                    evidence=self._capture("policy-violation"),
                )
                break

        result.finished_at = utc_now()
        self._capture("final")
        self.log.event("replay_finished", status=result.status.value)
        self.log.save_json("result.json", result.model_dump(exclude_none=True))
        return result

    # ------------------------------------------------------------ internals
    def _run_once(self, result: ReplayResult) -> None:
        for step in self.a.steps:
            report = self._execute_step(step)
            result.steps.append(report)

        # Checkpoint: never assume the last action worked.
        for cond in self.a.checkpoint:
            if not self._condition_holds(cond, timeout_ms=self.policy.action_timeout_ms):
                self._scan_outcomes(at_step="checkpoint")     # business outcome?
                self._scan_hard_errors(at_step="checkpoint")
                raise _HardFailure(FailureDetail(
                    step_id="checkpoint",
                    expected=f"checkpoint: {cond.describe()}",
                    observed=self._page_summary(),
                    evidence=self._capture("checkpoint-failed"),
                ))
        self.log.event("checkpoint_verified", conditions=[c.describe() for c in self.a.checkpoint])

        # Outputs
        outputs = {}
        for spec in self.a.outputs:
            try:
                outputs[spec.name] = self.surface.extract_text(spec.extract)
            except Exception as e:  # noqa: BLE001
                raise _HardFailure(FailureDetail(
                    step_id=f"extract:{spec.name}",
                    expected=f"output '{spec.name}' extractable ({spec.extract.description})",
                    observed=str(e)[:300],
                    evidence=self._capture(f"extract-{spec.name}-failed"),
                )) from e
        result.outputs = outputs
        result.status = ReplayStatus.SUCCESS
        self.log.event("outputs_extracted", outputs=outputs)

    def _execute_step(self, step: Step) -> StepReport:
        report = StepReport(step_id=step.id, action=step.action, status="failed")
        started = time.monotonic()
        attempts = 0
        while True:
            attempts += 1
            report.attempts = attempts
            try:
                self.log.event("step_start", step=step.id, action=step.action,
                               note=step.note, attempt=attempts)
                self._do_action(step, report)
                self.surface.wait_load(step.wait_after.load_state, step.wait_after.text_visible)
                # A page can answer the business question mid-flow.
                self._scan_hard_errors(at_step=step.id)
                self._scan_outcomes(at_step=step.id)
                report.status = "recovered" if report.recoveries_applied else "ok"
                report.duration_ms = int((time.monotonic() - started) * 1000)
                self.log.event("step_ok", step=step.id, strategy=report.strategy_used,
                               attempts=attempts, duration_ms=report.duration_ms)
                return report
            except (_BusinessOutcome, _HardFailure):
                report.duration_ms = int((time.monotonic() - started) * 1000)
                raise
            except PolicyViolation:
                raise
            except Exception as e:  # resolve/action failure -> triage
                self.log.event("step_error", step=step.id, attempt=attempts,
                               error=str(e)[:300])
                self._scan_hard_errors(at_step=step.id)
                self._scan_outcomes(at_step=step.id)
                rule = self._matching_recovery()
                if rule is not None:
                    self._apply_recovery(rule, report)
                    continue
                self._escalate_or_fail(step, e, report)
                # _escalate_or_fail raises _Restart/_Abort/_HardFailure; if it
                # returns, the operator asked us to retry in place.
                continue

    def _do_action(self, step: Step, report: StepReport) -> None:
        if step.action == "navigate":
            self.surface.navigate(self.a.bind(step.url or "", self.params))
            report.strategy_used = "url"
            return
        assert step.target is not None, f"step {step.id}: missing target"
        if step.action == "click":
            strat, _ = self.surface.click(step.target)
        elif step.action == "type":
            strat, _ = self.surface.type(step.target, self.a.bind(step.value or "", self.params))
        elif step.action == "select":
            strat, _ = self.surface.select(step.target, self.a.bind(step.value or "", self.params))
        elif step.action == "press":
            self.surface.press(step.key or "Enter")
            report.strategy_used = "key"
            return
        else:
            raise ValueError(f"unknown action '{step.action}'")
        report.strategy_used = strat.describe()
        # Drift signal: resolving via a fallback instead of the primary strategy.
        if step.target.strategies and _same_strategy(strat, step.target.strategies[0]) is False:
            self.log.event("drift_signal", step=step.id, used=strat.describe(),
                           primary=step.target.strategies[0].describe())

    # ---- condition scanning ---------------------------------------------
    def _condition_holds(self, cond: Condition, timeout_ms: int = 800) -> bool:
        if cond.text_visible:
            return self.surface.text_visible(cond.text_visible, timeout_ms=timeout_ms)
        if cond.element_visible:
            try:
                self.surface.resolve(cond.element_visible, timeout_ms=timeout_ms)
                return True
            except ResolveError:
                return False
        return False

    def _scan_outcomes(self, at_step: str) -> None:
        for det in self.a.outcome_detectors:
            if self._condition_holds(det.when):
                self.log.event("business_outcome_detected", detector=det.id, step=at_step)
                self._capture(f"outcome-{det.id}")
                raise _BusinessOutcome(OutcomeDetail(
                    id=det.id, message=det.message, at_step=at_step, outputs=det.outputs,
                ))

    def _scan_hard_errors(self, at_step: str) -> None:
        for det in self.a.hard_error_detectors:
            if self._condition_holds(det.when):
                self.log.event("hard_error_detected", detector=det.id, step=at_step)
                raise _HardFailure(FailureDetail(
                    step_id=at_step,
                    detector_id=det.id,
                    expected="no application error condition",
                    observed=f"{det.message} ({det.when.describe()})",
                    evidence=self._capture(f"hard-error-{det.id}"),
                ))

    def _matching_recovery(self):
        for rule in self.a.recovery_rules:
            used = self._recovery_uses.get(rule.id, 0)
            if used >= rule.max_attempts:
                continue
            if self._condition_holds(rule.when):
                return rule
        return None

    def _apply_recovery(self, rule, report: StepReport) -> None:
        self._recovery_uses[rule.id] = self._recovery_uses.get(rule.id, 0) + 1
        self.log.event("recovery_applied", rule=rule.id,
                       attempt=self._recovery_uses[rule.id])
        report.recoveries_applied.append(rule.id)
        if rule.action.kind == "click" and rule.action.target is not None:
            self.surface.click(rule.action.target)
        elif rule.action.kind == "reload":
            self.surface.page.reload()
        self.surface.wait_load()

    def _escalate_or_fail(self, step: Step, err: Exception, report: StepReport) -> None:
        evidence = self._capture(f"{step.id}-failed")
        if self.interventions is None:
            raise _HardFailure(FailureDetail(
                step_id=step.id,
                expected=self._expected_for(step),
                observed=f"{type(err).__name__}: {str(err)[:300]} | page: {self._page_summary()}",
                evidence=evidence,
            ))
        esc = self.interventions.request(
            reason=f"Replay blocked at step {step.id} ({step.action}): {str(err)[:200]}",
            context={
                "capability": self.a.capability_id,
                "step": step.id,
                "step_note": step.note,
                "expected": self._expected_for(step),
                "url": self.surface.page.url,
            },
            surface=self.surface,
        )
        esc.at_step = step.id
        if esc.resolution == "resumed":
            raise _Restart(esc)
        raise _Abort(esc)

    # ---- helpers ---------------------------------------------------------
    def _expected_for(self, step: Step) -> str:
        if step.action == "navigate":
            return f"navigate to {step.url}"
        tgt = step.target.strategies[0].describe() if step.target and step.target.strategies else "?"
        return f"{step.action} on {tgt} ({step.note})"

    def _page_summary(self) -> str:
        obs = self.surface.observe()
        return f"url={obs.url} title={obs.title!r} tree: {obs.aria[:400]}"

    def _capture(self, tag: str) -> list[str]:
        shot = self.log.dir / f"{tag}.png"
        self.surface.screenshot(str(shot))
        aria = self.log.save_text(f"{tag}.aria.txt", self.surface.observe().aria)
        return [str(shot), str(aria)]


def _same_strategy(a, b) -> bool:
    return (a.kind, a.role, a.name, a.label, a.text, a.selector, a.row_text, a.column_header) == \
           (b.kind, b.role, b.name, b.label, b.text, b.selector, b.row_text, b.column_header)
