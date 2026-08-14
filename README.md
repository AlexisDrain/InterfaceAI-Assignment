# Computer-Use Automation System

**The LLM figures out the task once. After that, it replays without the LLM. A human takes over when it gets stuck.**

The idea: point an AI agent at a legacy back-office web app (no API, messy HTML)
and give it a goal in plain English. The agent works out how to do it, and the
successful run gets saved as a small JSON file — a **capability artifact** — that
describes the steps, the inputs, and the outputs. From then on, the task runs by
replaying that file: no model, cheap, and predictable. The replay knows the
difference between "member not found" (a normal answer), "session expired"
(fix it and keep going), and "the app crashed" (stop and report). If it hits
something it can't handle, it pauses and hands the live browser session to a
human, then takes back over.

- Design write-up: [REPORT.md](REPORT.md)
- Example artifact + logs from real runs: [`evidence/`](evidence/)

## Layout

| Path | What it is |
|---|---|
| `cua/` | The system (Python): agent loop, artifact schema, compiler, replay engine, safety policy, escalation |
| `target_app/` | The practice target: a fake old-school "TellerCore 2000" teller portal, with switches to inject failures |
| `operator_console/` | Small TypeScript web UI a human uses during a handoff |
| `policy/policy.json` | Safety rules: which sites/actions are allowed, which buttons count as risky, what to redact |
| `profiles/` | Per-app knowledge (error pages, recovery steps) shared by every capability on that app |
| `artifacts/` | The saved capabilities — the catalog an agent can invoke |
| `evidence/` | Logs, results, and screenshots from real discovery and replay runs |

## Setup

Requires Python 3.11+. (Node is only needed if you want to rebuild the operator
console; a built copy is committed.)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Only **discovery** calls the model. Set a key for it:

```bash
# Windows (PowerShell):  $env:ANTHROPIC_API_KEY = "sk-ant-..."
export ANTHROPIC_API_KEY=sk-ant-...
```

Or copy `.env.example` to `.env` and put the key there — `.env` is gitignored
and the CLI loads it automatically (a real environment variable wins over it).

**No API key?** Everything except `discover` still works: replay, the catalog,
the policy checks, and the escalation flow. The target app runs locally;
nothing external is contacted.

## Demo path

Terminal 1 — start the target app:

```bash
python target_app/server.py        # http://127.0.0.1:8300
```

Terminal 2 — run the agent on a goal, then replay what it learned:

```bash
# 1) LLM-driven discovery -> writes artifacts/lookup_member_savings.v1.json (draft)
python -m cua.cli discover \
  --id lookup_member_savings \
  --goal "Sign in to the teller portal, look up the member by member number, and read their current Savings account balance." \
  --param teller_id=T-100 --param access_code=8421 --param member_id=12345 \
  --sensitive access_code --risk read_only

# 2) a human reviews and approves the artifact
python -m cua.cli approve artifacts/lookup_member_savings.v1.json

# 3) deterministic replay (no LLM) with a different member
python -m cua.cli replay artifacts/lookup_member_savings.v1.json \
  --param teller_id=T-100 --param access_code=8421 --param member_id=67890
```

Tip: add `--headed` to any `discover` or `replay` command to watch the browser
window while it works (use a scratch id like `--id watch_demo` so you don't
overwrite the approved artifact).

The replay prints a structured result (status, typed outputs, and a per-step
report showing which locator strategy found each element) and writes evidence
to `evidence/runs/<run_id>/`.

### Error & escalation demos

```bash
# a normal business answer, not a crash:
python -m cua.cli replay artifacts/lookup_member_savings.v1.json \
  --param teller_id=T-100 --param access_code=8421 --param member_id=99999
# -> status: business_outcome, outcome: member_not_found

# a recoverable error (auto-fixed, run still succeeds):
python -m cua.cli replay artifacts/lookup_member_savings.v1.json --inject session_expiry \
  --param teller_id=T-100 --param access_code=8421 --param member_id=12345

# a hard failure, with a screenshot + page snapshot to debug from:
python -m cua.cli replay artifacts/lookup_member_savings.v1.json \
  --param teller_id=T-100 --param access_code=8421 --param member_id=13013

# human-in-the-loop: an unknown popup blocks the run and forces a handoff
python -m cua.cli replay artifacts/lookup_member_savings.v1.json --inject unknown_modal --escalate \
  --param teller_id=T-100 --param access_code=8421 --param member_id=12345
# then EITHER open http://127.0.0.1:7100 (operator console: live screenshot,
# click/type on the live session, Resume/Abort), OR from another terminal:
python -m cua.cli operator click "Supervisor Override"
python -m cua.cli operator resume
```

Everything above (including a scripted operator for the escalation) runs in one go:

```bash
scripts/demo.sh        # or scripts\demo.ps1 on Windows
```

### Capability catalog (what an agent would see)

```bash
python -m cua.cli list
```

Prints each capability with its typed inputs/outputs, approval status, and risk
level — the surface an AI agent would use to find and invoke capabilities.

## Demo credentials & injected failures

The target app accepts any Teller ID / Access Code (the demo values `T-100` /
`8421` are fake and pre-filled). Members `12345` and `67890` exist; `99999` is
unknown (business outcome); `13013` triggers an error page (hard failure).
Failure injection: `python -m cua.cli inject slow|session_expiry|unknown_modal|none`.

No real credentials or PII anywhere. Sensitive parameters are redacted from all
logs and artifacts, and their values are never sent to the model (see REPORT.md,
Safety).
