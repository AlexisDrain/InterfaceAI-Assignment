#!/usr/bin/env bash
# End-to-end demo: discovery (LLM) -> approve -> deterministic replays covering
# the whole error taxonomy -> escalation with an automated operator.
# Prereqs: target app running (python target_app/server.py), ANTHROPIC_API_KEY set.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-python}"
[ -x .venv/bin/python ] && PY=.venv/bin/python

: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY is required for the discovery step}"

echo "=== 1. DISCOVERY (real LLM run): lookup member savings ==="
$PY -m cua.cli inject none
$PY -m cua.cli discover \
  --id lookup_member_savings \
  --goal "Sign in to the teller portal, look up the member by member number, and read their current Savings account balance." \
  --param teller_id=T-100 --param access_code=8421 --param member_id=12345 \
  --sensitive access_code \
  --risk read_only

ARTIFACT=$(ls artifacts/lookup_member_savings.v*.json | sort | tail -n1)

echo "=== 2. REVIEW + APPROVE the compiled artifact ==="
$PY -m cua.cli approve "$ARTIFACT"

echo "=== 3. REPLAY: happy path (deterministic, no LLM) ==="
$PY -m cua.cli replay "$ARTIFACT" --inject none \
  --param teller_id=T-100 --param access_code=8421 --param member_id=12345

echo "=== 4. REPLAY: expected business outcome (member not found) ==="
$PY -m cua.cli replay "$ARTIFACT" \
  --param teller_id=T-100 --param access_code=8421 --param member_id=99999 || true

echo "=== 5. REPLAY: recoverable runtime error (session expiry) ==="
$PY -m cua.cli replay "$ARTIFACT" --inject session_expiry \
  --param teller_id=T-100 --param access_code=8421 --param member_id=12345

echo "=== 6. REPLAY: hard failure (application error page) ==="
$PY -m cua.cli replay "$ARTIFACT" \
  --param teller_id=T-100 --param access_code=8421 --param member_id=13013 || true

echo "=== 7. REPLAY: escalation -> human handoff -> resume ==="
(
  # Automated operator: wait for the escalation, override the modal, resume.
  for _ in $(seq 1 120); do
    st=$(curl -s http://127.0.0.1:7100/api/state | grep -o '"status": *"[^"]*"' || true)
    case "$st" in *waiting*|*human*) break;; esac
    sleep 1
  done
  $PY -m cua.cli operator click "Supervisor Override"
  sleep 3
  $PY -m cua.cli operator resume
) &
$PY -m cua.cli replay "$ARTIFACT" --inject unknown_modal --escalate \
  --param teller_id=T-100 --param access_code=8421 --param member_id=12345
wait
$PY -m cua.cli inject none

echo "=== 8. Capability catalog ==="
$PY -m cua.cli list
echo "Evidence for every run is under evidence/runs/<run_id>/ (log.jsonl, result.json, screenshots)."
