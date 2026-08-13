# Computer-Use Automation System

**Discover with an LLM once. Replay deterministically forever. Escalate to a human when stuck.**

An AI agent figures out how to accomplish a goal in a legacy back-office web app
(no API, no clean DOM), and the successful run is compiled into a typed, versioned,
parameterized **capability artifact**. Production invocations replay that artifact
with **no model in the loop**, with explicit handling for the runtime errors that
actually happen (record-not-found, validation errors, session expiry, slow loads,
app errors, unexpected dialogs) and a real human-in-the-loop handoff for everything
else.

- Design write-up: [REPORT.md](REPORT.md)
- Example artifact + logs from real runs: [`evidence/`](evidence/)

## Layout

| Path | What it is |
|---|---|
| `cua/` | The system (Python): agent loop, artifact schema, compiler, replay engine, policy, escalation |
| `target_app/` | Stand-in target: a deliberately legacy "TellerCore 2000" teller portal with failure injection |
| `operator_console/` | TypeScript operator console for human-in-the-loop handoffs |
| `policy/policy.json` | Safety policy: origin/action allowlists, risky-control patterns, redaction rules |
| `profiles/` | App profiles: per-application detectors/recoveries shared by all capabilities |
| `artifacts/` | Saved capability artifacts (the invocable catalog) |
| `evidence/` | Structured logs, results, and screenshots from discovery + replay runs |

## Setup

Requires Python 3.11+. (Node is only needed if you want to rebuild the operator
console bundle; a built copy is committed.)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Only the **discovery** path calls the model. Set a key for it:

```bash
# Windows (PowerShell):  $env:ANTHROPIC_API_KEY = "sk-ant-..."
export ANTHROPIC_API_KEY=sk-ant-...
```

**Running without live services:** deterministic replay, the capability catalog,
the policy layer, and the escalation flow all work with no API key — only
`discover` needs one. The target app is local; nothing external is contacted.

## Demo path

Terminal 1 — start the target app:

```bash
python target_app/server.py        # http://127.0.0.1:8300
```

Terminal 2 — run the agent on a goal, then replay the resulting artifact:

```bash
# 1) LLM-driven discovery -> compiles artifacts/lookup_member_savings.v1.json (draft)
python -m cua.cli discover \
  --id lookup_member_savings \
  --goal "Sign in to the teller portal, look up the member by member number, and read their current Savings account balance." \
  --param teller_id=T-100 --param access_code=8421 --param member_id=12345 \
  --sensitive access_code --risk read_only

# 2) human review gate
python -m cua.cli approve artifacts/lookup_member_savings.v1.json

# 3) deterministic replay (no LLM) with new parameters
python -m cua.cli replay artifacts/lookup_member_savings.v1.json \
  --param teller_id=T-100 --param access_code=8421 --param member_id=67890
```

The replay prints the structured result contract (status, typed outputs, per-step
report including which locator strategy resolved) and writes evidence to
`evidence/runs/<run_id>/`.

### Error & escalation demos

```bash
# expected business outcome, not a crash:
python -m cua.cli replay artifacts/lookup_member_savings.v1.json \
  --param teller_id=T-100 --param access_code=8421 --param member_id=99999
# -> status: business_outcome, outcome: member_not_found

# recoverable runtime error (auto-recovered, run still succeeds):
python -m cua.cli replay artifacts/lookup_member_savings.v1.json --inject session_expiry \
  --param teller_id=T-100 --param access_code=8421 --param member_id=12345

# hard failure with debuggable evidence (screenshot + accessibility snapshot):
python -m cua.cli replay artifacts/lookup_member_savings.v1.json \
  --param teller_id=T-100 --param access_code=8421 --param member_id=13013

# human-in-the-loop: an unknown blocking modal forces an escalation
python -m cua.cli replay artifacts/lookup_member_savings.v1.json --inject unknown_modal --escalate \
  --param teller_id=T-100 --param access_code=8421 --param member_id=12345
# then EITHER open http://127.0.0.1:7100 (operator console UI: live screenshot,
# click/type on the live session, Resume/Abort), OR from another terminal:
python -m cua.cli operator click "Supervisor Override"
python -m cua.cli operator resume
```

Everything above (including an automated operator for the escalation) is scripted:

```bash
scripts/demo.sh        # or scripts\demo.ps1 on Windows
```

### Capability catalog (agent-facing surface)

```bash
python -m cua.cli list
```

Prints each capability with its typed inputs/outputs, approval status, and risk
level — the surface an AI agent would use to discover and invoke capabilities.

## Demo credentials & injected failures

The target app accepts any Teller ID / Access Code (demo values `T-100` / `8421`
are fake). Members `12345` and `67890` exist; `99999` is unknown (business
outcome); `13013` triggers a 500 error page (hard failure). Failure injection:
`python -m cua.cli inject slow|session_expiry|unknown_modal|none`.

No real credentials or PII are used anywhere; sensitive parameters are redacted
from all logs/artifacts and are never sent to the model (see REPORT.md, Safety).
