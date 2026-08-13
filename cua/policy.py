"""Safety policy: allowlist enforcement, risky-action classification, redaction.

The policy is enforced in the *surface layer* (the code that touches the
browser), not in the prompt. The model can ask for anything; the surface
refuses anything outside policy. That way discovery and replay share one
enforcement point and a prompt-injection cannot widen the blast radius.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field


class PolicyViolation(Exception):
    """Raised by the surface when an action falls outside the policy."""


class RiskyActionBlocked(PolicyViolation):
    """A risky/irreversible control was about to be operated."""


class Policy(BaseModel):
    allowed_origins: list[str]
    allowed_actions: list[str]
    risky_action_patterns: list[str] = Field(default_factory=list)
    risky_action_mode: Literal["block", "escalate"] = "block"
    sensitive_value_patterns: list[str] = Field(default_factory=list)
    max_discovery_steps: int = 30
    action_timeout_ms: int = 10000
    navigation_timeout_ms: int = 15000

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        raw = {k: v for k, v in raw.items() if not k.startswith("$")}
        return cls.model_validate(raw)

    # ---- allowlist -------------------------------------------------------
    def check_origin(self, url: str) -> None:
        origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        if origin not in self.allowed_origins:
            raise PolicyViolation(
                f"origin '{origin}' is not in the allowlist {self.allowed_origins}"
            )

    def check_action(self, action: str) -> None:
        if action not in self.allowed_actions:
            raise PolicyViolation(f"action '{action}' is not permitted by policy")

    # ---- risky / irreversible actions -----------------------------------
    def risky_match(self, control_text: str) -> str | None:
        """Return the matching pattern if this control is classified risky."""
        for pat in self.risky_action_patterns:
            if re.search(pat, control_text or ""):
                return pat
        return None

    def check_click_allowed(self, control_text: str) -> None:
        pat = self.risky_match(control_text)
        if pat:
            raise RiskyActionBlocked(
                f"control '{control_text.strip()}' matches risky pattern {pat!r}; "
                f"policy mode is '{self.risky_action_mode}'"
            )


class Redactor:
    """Removes sensitive material from anything we persist (logs, artifacts,
    evidence). Two sources of truth: concrete sensitive values (e.g. the bound
    value of a `sensitive: true` parameter) and configured patterns (PINs,
    SSNs, ...). Screenshots are stored only in local evidence directories.
    """

    MASK = "***REDACTED***"

    def __init__(self, policy: Policy):
        self._values: list[str] = []
        self._patterns = [re.compile(p) for p in policy.sensitive_value_patterns]

    def register_value(self, value: str) -> None:
        if value and value not in self._values:
            self._values.append(value)
            self._values.sort(key=len, reverse=True)  # longest first

    def redact(self, text: str) -> str:
        if not isinstance(text, str):
            return text
        for v in self._values:
            text = text.replace(v, self.MASK)
        for pat in self._patterns:
            text = pat.sub(self.MASK, text)
        return text

    def redact_obj(self, obj):
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact_obj(v) for v in obj]
        return obj
