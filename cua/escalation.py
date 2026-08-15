"""Human-in-the-loop escalation: pause automation, hand the LIVE session to an
operator, record what they did, and hand control back.

Control-transfer model (a small explicit state machine):

    AGENT_CONTROL --(stuck/blocked/risky)--> PAUSED
    PAUSED --operator opens console-------> HUMAN_CONTROL
    HUMAN_CONTROL --resume----------------> AGENT_CONTROL   (replay restarts)
    HUMAN_CONTROL --abort-----------------> ABORTED

The operator drives the SAME live browser session the automation was using --
not a fresh one. Two channels, both recorded as structured human_action events:

  1. The operator console (TypeScript UI served on http://127.0.0.1:7100) posts
     structured commands (click-by-text, type, refresh screenshot) which this
     manager executes against the live Playwright page.
  2. If the run is headed, the operator can interact with the browser window
     directly; an injected DOM listener reports every click/change back through
     a Playwright binding.

In production this console would be a real operator workspace behind auth and
the browser session would live in a remote pool (CDP/VNC); the seam -- pause,
expose session, record actions, signal resume -- is exactly what is built here.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal, Optional

from .policy import Policy
from .schema import ElementTarget, EscalationReport, LocatorStrategy, utc_now
from .surface import BrowserSurface
from .tracelog import RunLogger

CONSOLE_DIST = Path(__file__).resolve().parent.parent / "operator_console" / "dist"


class InterventionManager:
    def __init__(self, policy: Policy, logger: RunLogger, port: int = 7100,
                 wait_timeout_s: int = 900):
        self.policy = policy
        self.log = logger
        self.port = port
        self.wait_timeout_s = wait_timeout_s
        self._commands: "queue.Queue[dict]" = queue.Queue()
        self._state: dict = {"status": "idle", "intervention": None}
        self._screenshot_path: Path | None = None
        self._server_started = False
        self._lock = threading.Lock()
        self._current_report: EscalationReport | None = None

    # ---- called by the surface when a human acts in the live browser -----
    def record_human_action(self, detail: dict) -> None:
        with self._lock:
            if self._current_report is not None:
                action = {"channel": "browser", "at": utc_now(), **detail}
                self._current_report.human_actions.append(action)
                self.log.event("human_action", **action)

    # ---- main entry: block until the operator resolves -------------------
    def request(self, reason: str, context: dict, surface: BrowserSurface) -> EscalationReport:
        intervention_id = f"iv-{uuid.uuid4().hex[:8]}"
        report = EscalationReport(
            intervention_id=intervention_id, reason=reason, resolution="timeout",
        )
        self._current_report = report
        iv_dir = self.log.dir / intervention_id
        iv_dir.mkdir(parents=True, exist_ok=True)
        self._screenshot_path = iv_dir / "screenshot.png"
        surface.screenshot(str(self._screenshot_path))

        self._state = {
            "status": "waiting_for_operator",
            "intervention": {
                "id": intervention_id,
                "reason": reason,
                "context": context,
                "url": surface.page.url,
                "raised_at": utc_now(),
            },
        }
        (iv_dir / "request.json").write_text(
            json.dumps(self._state["intervention"], indent=2), encoding="utf-8")
        self._ensure_server()
        self.log.event("intervention_raised", id=intervention_id, reason=reason,
                       context=context,
                       console=f"http://127.0.0.1:{self.port}/")
        print(f"\n[ESCALATION] {reason}")
        print(f"[ESCALATION] Operator console: http://127.0.0.1:{self.port}/  "
              f"(or: python -m cua.cli operator <cmd>)")

        surface.human_control = True
        self._state["status"] = "human_in_control"
        deadline = time.monotonic() + self.wait_timeout_s
        try:
            while time.monotonic() < deadline:
                try:
                    cmd = self._commands.get(timeout=1.0)
                except queue.Empty:
                    continue
                resolution = self._handle_command(cmd, surface, report)
                surface.screenshot(str(self._screenshot_path))
                if resolution:
                    report.resolution = resolution
                    break
        finally:
            surface.human_control = False
            self._current_report = None
            self._state = {"status": "idle", "intervention": None}

        self.log.event("intervention_resolved", id=intervention_id,
                       resolution=report.resolution,
                       human_actions=len(report.human_actions))
        (iv_dir / "resolution.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8")
        return report

    def _handle_command(self, cmd: dict, surface: BrowserSurface,
                        report: EscalationReport) -> Optional[Literal["resumed", "aborted"]]:
        op = cmd.get("op")
        action = {"channel": "console", "at": utc_now(), **cmd}
        if op not in ("resume", "abort"):
            report.human_actions.append(action)
        self.log.event("operator_command", **cmd)
        try:
            if op == "click":
                target = ElementTarget(
                    description=f"operator click: {cmd.get('text', '')}",
                    strategies=[
                        LocatorStrategy(kind="role", role="button", name=cmd.get("text")),
                        LocatorStrategy(kind="text", text=cmd.get("text")),
                    ],
                )
                surface.click(target)
                surface.wait_load()
            elif op == "type":
                target = ElementTarget(
                    description=f"operator type into: {cmd.get('field', '')}",
                    strategies=[LocatorStrategy(kind="label", label=cmd.get("field"))],
                )
                surface.type(target, cmd.get("value", ""))
            elif op == "refresh":
                pass  # screenshot refresh happens in the caller
            elif op == "resume":
                return "resumed"
            elif op == "abort":
                return "aborted"
        except Exception as e:  # noqa: BLE001 - operator sees the error, decides next move
            self.log.event("operator_command_failed", op=op, error=str(e)[:200])
        return None

    # ---- tiny HTTP server for the operator console -----------------------
    def _ensure_server(self) -> None:
        if self._server_started:
            return
        manager = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status: int, body: bytes, ctype: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/" or self.path.startswith("/index"):
                    f = CONSOLE_DIST / "index.html"
                    self._send(200, f.read_bytes() if f.exists()
                               else b"operator console bundle missing", "text/html")
                elif self.path.startswith("/console.js"):
                    f = CONSOLE_DIST / "console.js"
                    self._send(200, f.read_bytes() if f.exists() else b"",
                               "application/javascript")
                elif self.path.startswith("/api/state"):
                    self._send(200, json.dumps(manager._state).encode(), "application/json")
                elif self.path.startswith("/screenshot.png"):
                    p = manager._screenshot_path
                    if p and p.exists():
                        self._send(200, p.read_bytes(), "image/png")
                    else:
                        self._send(404, b"", "image/png")
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self):
                if self.path.startswith("/api/command"):
                    length = int(self.headers.get("Content-Length", 0))
                    try:
                        cmd = json.loads(self.rfile.read(length) or b"{}")
                    except json.JSONDecodeError:
                        self._send(400, b"bad json", "text/plain")
                        return
                    manager._commands.put(cmd)
                    self._send(200, b'{"ok": true}', "application/json")
                else:
                    self._send(404, b"not found", "text/plain")

            def log_message(self, format: str, *args) -> None:  # noqa: A002 - matches stdlib signature
                pass

        server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._server_started = True
