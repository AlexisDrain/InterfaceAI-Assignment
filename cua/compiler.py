"""Compile a discovery trace into a reusable, reviewable capability artifact.

This step is deterministic code, not the model: the model's job ended with the
trace. The compiler
  * parameterizes literal values back into {{param}} placeholders,
  * builds a *ranked* locator-strategy list per step: the semantic target the
    model used first, then structural fallbacks captured at action time
    (visible text, generated CSS path),
  * attaches the checkpoint, declared outputs, and provenance,
  * merges in the app profile (outcome detectors / recovery rules / hard-error
    detectors shared by every capability recorded against this application).

Artifacts are born as "draft"; replay refuses drafts unless explicitly allowed,
so a human reviews the compiled flow before it becomes an invocable capability.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from .agent import DiscoveryOutcome, TraceStep
from .schema import (
    Artifact, Condition, ElementTarget, HardErrorDetector, LocatorStrategy,
    OutcomeDetector, OutputSpec, ParamSpec, Provenance, RecoveryAction,
    RecoveryRule, RiskSpec, Step, WaitSpec, utc_now,
)


def _strategy_key(s: LocatorStrategy) -> tuple:
    return (s.kind, s.role, s.name, s.label, s.text, s.selector, s.row_text, s.column_header)


def _build_target(step: TraceStep) -> ElementTarget:
    """Primary semantic strategies (as the model targeted them), then fallbacks
    derived from the element descriptor captured at action time."""
    strategies: list[LocatorStrategy] = []
    args = step.llm_target or {}
    if args.get("role"):
        strategies.append(LocatorStrategy(kind="role", role=args["role"], name=args.get("name")))
    if args.get("label"):
        strategies.append(LocatorStrategy(kind="label", label=args["label"]))
    if args.get("text"):
        strategies.append(LocatorStrategy(kind="text", text=args["text"]))
    if args.get("css"):
        strategies.append(LocatorStrategy(kind="css", selector=args["css"]))

    d = step.descriptor or {}
    if d.get("text") and step.action == "click" and len(d["text"]) <= 40:
        strategies.append(LocatorStrategy(kind="text", text=d["text"]))
    if d.get("name_attr") and d.get("tag"):
        strategies.append(LocatorStrategy(kind="css", selector=f'{d["tag"]}[name="{d["name_attr"]}"]'))
    if d.get("css_path"):
        strategies.append(LocatorStrategy(kind="css", selector=d["css_path"]))

    seen, unique = set(), []
    for s in strategies:
        k = _strategy_key(s)
        if k not in seen:
            seen.add(k)
            unique.append(s)
    return ElementTarget(description=step.why or "", strategies=unique)


def _templatize(text: str, params: dict[str, str], param_specs: list[ParamSpec]) -> str:
    """If the model typed a literal parameter value instead of a placeholder,
    fold it back into {{name}}. Longest values first to avoid partial hits."""
    if not text:
        return text
    non_sensitive = {p.name for p in param_specs if not p.sensitive}
    for name, value in sorted(params.items(), key=lambda kv: -len(str(kv[1]))):
        if name in non_sensitive and value and value in text:
            text = text.replace(value, f"{{{{{name}}}}}")
    return text


def load_app_profile(path: str | Path) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("$")}


def compile_artifact(
    outcome: DiscoveryOutcome,
    *,
    capability_id: str,
    name: str,
    description: str,
    goal: str,
    entry_url: str,
    params: dict[str, str],
    param_specs: list[ParamSpec],
    discovery_run_id: str,
    app_profile: dict | None = None,
    risk: RiskSpec | None = None,
) -> Artifact:
    profile = app_profile or {}
    base_url = profile.get("base_url") or f"{urlparse(entry_url).scheme}://{urlparse(entry_url).netloc}"

    steps: list[Step] = []
    entry_path = entry_url[len(base_url):] if entry_url.startswith(base_url) else entry_url
    steps.append(Step(id="s1", action="navigate", note="open the application entry point",
                      url="{{base_url}}" + entry_path))

    for i, t in enumerate(outcome.trace, start=2):
        sid = f"s{i}"
        if t.action == "navigate":
            url = t.url or ""
            if url.startswith(base_url):
                url = "{{base_url}}" + url[len(base_url):]
            steps.append(Step(id=sid, action="navigate", note=t.why, url=_templatize(url, params, param_specs)))
        elif t.action == "click":
            steps.append(Step(id=sid, action="click", note=t.why, target=_build_target(t)))
        elif t.action == "type":
            steps.append(Step(id=sid, action="type", note=t.why, target=_build_target(t),
                              value=_templatize(t.value or "", params, param_specs)))
        elif t.action == "select":
            steps.append(Step(id=sid, action="select", note=t.why, target=_build_target(t),
                              value=_templatize(t.option or "", params, param_specs)))
        elif t.action == "press":
            steps.append(Step(id=sid, action="press", note=t.why, key=t.value))

    outputs = [
        OutputSpec(
            name=e["output_name"],
            description=e.get("description", ""),
            extract=ElementTarget.model_validate(e["target"]),
        )
        for e in outcome.extracts
    ]

    artifact = Artifact(
        capability_id=capability_id,
        name=name,
        description=description,
        base_url=base_url,
        inputs=param_specs,
        outputs=outputs,
        steps=steps,
        checkpoint=[Condition(text_visible=outcome.checkpoint_text)] if outcome.checkpoint_text else [],
        outcome_detectors=[
            OutcomeDetector.model_validate({**d, "origin": "app_profile"})
            for d in profile.get("outcome_detectors", [])
        ],
        hard_error_detectors=[
            HardErrorDetector.model_validate({**d, "origin": "app_profile"})
            for d in profile.get("hard_error_detectors", [])
        ],
        recovery_rules=[
            RecoveryRule.model_validate({**r, "origin": "app_profile"})
            for r in profile.get("recovery_rules", [])
        ],
        risk=risk or RiskSpec(),
        provenance=Provenance(
            discovered_at=utc_now(),
            model=outcome.model,
            discovery_run_id=discovery_run_id,
            app_profile_id=profile.get("app_id"),
            goal=goal,
        ),
    )
    return artifact


def save_artifact(artifact: Artifact, dir_: str | Path = "artifacts") -> Path:
    path = Path(dir_) / f"{artifact.capability_id}.v{artifact.version}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(artifact.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    return path
