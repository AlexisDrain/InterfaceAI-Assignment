"""Typed artifact schema and replay result contract.

The artifact is the product of a discovery run: a versioned, reviewable,
parameterized description of a UI flow that an AI agent can invoke as a
capability. It is deliberately decoupled from the raw model transcript --
nothing in here requires an LLM to execute.

Design notes (see REPORT.md for the full rationale):

* Element targeting is a *ranked list of strategies*, semantic-first
  (role/name, label) with structural fallbacks (text, CSS). Replay records
  which strategy actually resolved, which doubles as a drift signal.
* Values are template strings ("{{member_id}}") bound at invocation time from
  typed input parameters. Sensitive parameters are never persisted.
* The artifact separates three failure classes explicitly:
    - outcome_detectors:    expected business outcomes (a *result*, not a crash)
    - recovery_rules:       known recoverable conditions (interstitials, retries)
    - hard_error_detectors: conditions that must stop the run with evidence
* `app_profile` knowledge (detectors/recoveries shared by every capability on
  the same vendor product) is merged in at compile time and marked with its
  origin, which is the seam for multi-tenant reuse.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------
# Element targeting
# --------------------------------------------------------------------------
class LocatorStrategy(BaseModel):
    """One way to find a control. Tried in order; first visible match wins."""

    kind: Literal["role", "label", "text", "css", "row_cell"]
    # role
    role: Optional[str] = None          # ARIA role, e.g. "button", "textbox"
    name: Optional[str] = None          # accessible name (substring match)
    # label
    label: Optional[str] = None         # associated <label> text
    # text
    text: Optional[str] = None          # visible text content
    # css (last resort -- least stable on legacy surfaces)
    selector: Optional[str] = None
    # row_cell: table cell addressed semantically (row text x column header),
    # built for legacy table-soup UIs where cells have no ids or classes.
    row_text: Optional[str] = None
    column_header: Optional[str] = None

    def describe(self) -> str:
        if self.kind == "role":
            return f'role={self.role} name="{self.name}"'
        if self.kind == "label":
            return f'label="{self.label}"'
        if self.kind == "text":
            return f'text="{self.text}"'
        if self.kind == "css":
            return f"css={self.selector}"
        return f'cell(row~"{self.row_text}", col="{self.column_header}")'


class ElementTarget(BaseModel):
    description: str = ""               # human-readable, for reviewers
    strategies: list[LocatorStrategy]


# --------------------------------------------------------------------------
# Inputs / outputs (the capability's call signature)
# --------------------------------------------------------------------------
class ParamSpec(BaseModel):
    name: str
    type: Literal["string", "number"] = "string"
    description: str = ""
    required: bool = True
    sensitive: bool = False             # sensitive => redacted in logs, never
                                        # persisted in artifacts or evidence


class OutputSpec(BaseModel):
    name: str
    type: Literal["string", "number", "boolean"] = "string"
    description: str = ""
    extract: ElementTarget              # inner text of the resolved element


# --------------------------------------------------------------------------
# Steps, waits, assertions
# --------------------------------------------------------------------------
class WaitSpec(BaseModel):
    load_state: Literal["load", "domcontentloaded", "networkidle"] = "load"
    text_visible: Optional[str] = None  # additionally wait for this text


class Condition(BaseModel):
    """A detectable page condition (assertions and detectors share this)."""

    text_visible: Optional[str] = None
    element_visible: Optional[ElementTarget] = None

    def describe(self) -> str:
        if self.text_visible:
            return f'text visible: "{self.text_visible}"'
        if self.element_visible:
            return f"element visible: {self.element_visible.description}"
        return "(empty condition)"


class Step(BaseModel):
    id: str
    action: Literal["navigate", "click", "type", "select", "press"]
    note: str = ""                      # what this step accomplishes (review aid)
    target: Optional[ElementTarget] = None
    value: Optional[str] = None         # template string; "{{param}}" placeholders
    url: Optional[str] = None           # navigate only; template string
    key: Optional[str] = None           # press only
    wait_after: WaitSpec = Field(default_factory=WaitSpec)


# --------------------------------------------------------------------------
# Error taxonomy carried by the artifact
# --------------------------------------------------------------------------
class OutcomeDetector(BaseModel):
    """An EXPECTED business outcome the caller needs to know about.

    'No such member' is a legitimate result, not a crash; conflating the two is
    the classic design mistake this type exists to prevent.
    """

    id: str
    when: Condition
    classification: Literal["business_outcome"] = "business_outcome"
    message: str
    outputs: dict[str, Any] = Field(default_factory=dict)
    origin: Literal["app_profile", "capability"] = "capability"


class HardErrorDetector(BaseModel):
    id: str
    when: Condition
    message: str
    origin: Literal["app_profile", "capability"] = "capability"


class RecoveryAction(BaseModel):
    kind: Literal["click", "reload"]
    target: Optional[ElementTarget] = None


class RecoveryRule(BaseModel):
    """A known, safe, mechanical recovery -- never open-ended."""

    id: str
    when: Condition
    action: RecoveryAction
    max_attempts: int = 2
    origin: Literal["app_profile", "capability"] = "capability"


class RiskSpec(BaseModel):
    level: Literal["read_only", "prepares_change", "makes_change"] = "read_only"
    stops_before: Optional[str] = None  # irreversible control the flow must never touch
    rationale: str = ""


class Provenance(BaseModel):
    discovered_at: str
    model: str
    discovery_run_id: str
    app_profile_id: Optional[str] = None
    goal: str


# --------------------------------------------------------------------------
# The artifact itself
# --------------------------------------------------------------------------
class Artifact(BaseModel):
    schema_version: str = SCHEMA_VERSION
    capability_id: str                  # stable machine name, e.g. "lookup_member_savings"
    name: str
    description: str
    version: int = 1
    status: Literal["draft", "approved"] = "draft"

    base_url: str                       # may be overridden per tenant at invoke time
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    checkpoint: list[Condition] = Field(default_factory=list)

    outcome_detectors: list[OutcomeDetector] = Field(default_factory=list)
    hard_error_detectors: list[HardErrorDetector] = Field(default_factory=list)
    recovery_rules: list[RecoveryRule] = Field(default_factory=list)

    risk: RiskSpec = Field(default_factory=RiskSpec)
    provenance: Optional[Provenance] = None

    # ---- invocation-time helpers ----------------------------------------
    def bind(self, template: str, params: dict[str, str]) -> str:
        """Substitute {{name}} placeholders; unknown placeholders are an error."""

        def repl(m: re.Match) -> str:
            key = m.group(1)
            if key == "base_url":
                return self.base_url
            if key not in params:
                raise KeyError(f"artifact references undeclared parameter '{key}'")
            return str(params[key])

        return re.sub(r"\{\{(\w+)\}\}", repl, template)

    def validate_params(self, params: dict[str, str]) -> dict[str, str]:
        known = {p.name: p for p in self.inputs}
        for k in params:
            if k not in known:
                raise ValueError(f"unknown parameter '{k}' (declared: {list(known)})")
        for spec in self.inputs:
            if spec.required and spec.name not in params:
                raise ValueError(f"missing required parameter '{spec.name}'")
            if spec.name in params and spec.type == "number":
                float(params[spec.name])  # raises ValueError if not numeric
        return params

    def sensitive_values(self, params: dict[str, str]) -> list[str]:
        names = {p.name for p in self.inputs if p.sensitive}
        return [v for k, v in params.items() if k in names and v]


# --------------------------------------------------------------------------
# Replay result contract (what the calling agent gets back)
# --------------------------------------------------------------------------
class ReplayStatus(str, Enum):
    SUCCESS = "success"                    # checkpoint verified, outputs returned
    BUSINESS_OUTCOME = "business_outcome"  # expected non-happy-path result
    HARD_FAILURE = "hard_failure"          # stop; debuggable error with evidence
    ABORTED = "aborted"                    # operator or policy aborted the run


class StepReport(BaseModel):
    step_id: str
    action: str
    status: Literal["ok", "recovered", "failed", "skipped"]
    strategy_used: Optional[str] = None    # which locator strategy resolved (drift signal)
    attempts: int = 1
    duration_ms: int = 0
    recoveries_applied: list[str] = Field(default_factory=list)


class FailureDetail(BaseModel):
    step_id: Optional[str] = None
    expected: str
    observed: str
    detector_id: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)  # file paths


class OutcomeDetail(BaseModel):
    id: str
    message: str
    at_step: Optional[str] = None
    outputs: dict[str, Any] = Field(default_factory=dict)


class EscalationReport(BaseModel):
    intervention_id: str
    reason: str
    at_step: Optional[str] = None
    resolution: Literal["resumed", "aborted", "timeout"]
    human_actions: list[dict[str, Any]] = Field(default_factory=list)


class ReplayResult(BaseModel):
    run_id: str
    capability_id: str
    artifact_version: int
    status: ReplayStatus
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: Optional[OutcomeDetail] = None
    failure: Optional[FailureDetail] = None
    steps: list[StepReport] = Field(default_factory=list)
    escalations: list[EscalationReport] = Field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    evidence_dir: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
