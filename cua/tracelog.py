"""Structured, redacted run logging -> evidence/runs/<run_id>/log.jsonl.

Every run (discovery or replay) gets an evidence directory containing:
  log.jsonl        -- ordered structured events: what happened and why
  result.json      -- the final result contract / artifact pointer
  *.png, *.aria.txt -- screenshots + accessibility snapshots on failure/finish

All persisted text passes through the Redactor first.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from .policy import Redactor
from .schema import utc_now


class RunLogger:
    def __init__(self, kind: str, redactor: Redactor, base_dir: str | Path = "evidence/runs"):
        self.run_id = f"{kind}-{utc_now().replace(':', '').replace('+0000', 'Z')}-{uuid.uuid4().hex[:6]}"
        self.dir = Path(base_dir) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._redactor = redactor
        self._fh = open(self.dir / "log.jsonl", "a", encoding="utf-8")

    def event(self, type_: str, **data) -> None:
        rec = {"ts": utc_now(), "type": type_, **self._redactor.redact_obj(data)}
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()

    def save_json(self, name: str, obj) -> Path:
        path = self.dir / name
        path.write_text(
            json.dumps(self._redactor.redact_obj(obj), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def save_text(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(self._redactor.redact(text), encoding="utf-8")
        return path

    def close(self) -> None:
        self._fh.close()
