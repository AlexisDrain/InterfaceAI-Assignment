# Evidence

Every run — discovery or replay — writes a directory under `runs/<run_id>/`:

| File | Contents |
|---|---|
| `log.jsonl` | Ordered structured events: agent actions and model notes (discovery), step execution, which locator strategy resolved, recoveries applied, detectors fired, escalations, operator/human actions. All redacted. |
| `result.json` | The final result contract (replay) or discovery summary + artifact pointer |
| `*.png` / `*.aria.txt` | Screenshot + accessibility snapshot captured on failures, detected outcomes, and at run end |
| `iv-*/` | Escalations: `request.json` (context sent to the operator) and `resolution.json` (how it ended, every human action recorded) |

The demo path (`scripts/demo.sh` / `scripts/demo.ps1`) produces:

1. a **discovery run** — the genuine LLM-driven run required by the brief,
2. a **happy-path replay** of the compiled artifact (different member than discovery),
3. a **business-outcome replay** (member not found),
4. a **recovered replay** (injected session expiry),
5. a **hard-failure replay** (application error page) with screenshot evidence,
6. an **escalation replay** (unknown blocking modal → human handoff → resume).

The saved example artifact lives in `../artifacts/`.
