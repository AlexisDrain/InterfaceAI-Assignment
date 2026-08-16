# End-to-end demo: discovery (LLM) -> approve -> deterministic replays covering
# the whole error taxonomy -> escalation with an automated operator.
# Prereqs: target app running (python target_app/server.py), ANTHROPIC_API_KEY set.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

if (-not $env:ANTHROPIC_API_KEY -and -not (Test-Path ".env")) {
  Write-Host "ANTHROPIC_API_KEY is required for discovery (set the env var, or put it in .env)." -ForegroundColor Yellow
  exit 1
}

Write-Host "`n=== 1. DISCOVERY (real LLM run): lookup member savings ===" -ForegroundColor Cyan
& $py -m cua.cli inject none
& $py -m cua.cli discover `
  --id lookup_member_savings `
  --goal "Sign in to the teller portal, look up the member by member number, and read their current Savings account balance." `
  --param teller_id=T-100 --param access_code=8421 --param member_id=12345 `
  --sensitive access_code `
  --risk read_only
if ($LASTEXITCODE -ne 0) { Write-Host "discovery failed"; exit 1 }

$artifact = Get-ChildItem artifacts\lookup_member_savings.v*.json | Sort-Object Name | Select-Object -Last 1

Write-Host "`n=== 2. REVIEW + APPROVE the compiled artifact ===" -ForegroundColor Cyan
& $py -m cua.cli approve $artifact.FullName

Write-Host "`n=== 3. REPLAY: happy path (deterministic, no LLM) ===" -ForegroundColor Cyan
& $py -m cua.cli replay $artifact.FullName --inject none `
  --param teller_id=T-100 --param access_code=8421 --param member_id=12345

Write-Host "`n=== 4. REPLAY: expected business outcome (member not found) ===" -ForegroundColor Cyan
& $py -m cua.cli replay $artifact.FullName `
  --param teller_id=T-100 --param access_code=8421 --param member_id=99999

Write-Host "`n=== 5. REPLAY: recoverable runtime error (session expiry) ===" -ForegroundColor Cyan
& $py -m cua.cli replay $artifact.FullName --inject session_expiry `
  --param teller_id=T-100 --param access_code=8421 --param member_id=12345

Write-Host "`n=== 6. REPLAY: hard failure (application error page) ===" -ForegroundColor Cyan
& $py -m cua.cli replay $artifact.FullName `
  --param teller_id=T-100 --param access_code=8421 --param member_id=13013

Write-Host "`n=== 7. REPLAY: escalation -> human handoff -> resume ===" -ForegroundColor Cyan
$job = Start-Job -ScriptBlock {
  param($root)
  Set-Location $root
  $py = ".\.venv\Scripts\python.exe"; if (-not (Test-Path $py)) { $py = "python" }
  # Wait for the escalation to be raised, then act as the operator.
  $deadline = (Get-Date).AddSeconds(120)
  while ((Get-Date) -lt $deadline) {
    try {
      $st = Invoke-RestMethod http://127.0.0.1:7100/api/state -TimeoutSec 2
      if ($st.status -ne "idle") { break }
    } catch {}
    Start-Sleep 1
  }
  & $py -m cua.cli operator click "Supervisor Override"
  Start-Sleep 3
  & $py -m cua.cli operator resume
} -ArgumentList (Get-Location).Path
& $py -m cua.cli replay $artifact.FullName --inject unknown_modal --escalate `
  --param teller_id=T-100 --param access_code=8421 --param member_id=12345
Receive-Job $job -Wait | Out-Null; Remove-Job $job -Force
& $py -m cua.cli inject none

Write-Host "`n=== 8. Capability catalog ===" -ForegroundColor Cyan
& $py -m cua.cli list
Write-Host "`nEvidence for every run is under evidence\runs\<run_id>\ (log.jsonl, result.json, screenshots)."
