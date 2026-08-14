# Design Report

## 1. Architecture

The core idea I built everything around: **the model figures the task out once;
a compiler turns what it did into a typed artifact; production runs replay that
artifact with no model involved.**

```
goal + typed params                    artifact + typed params
      │                                        │
      ▼                                        ▼
┌─ DiscoveryAgent ─┐   trace    ┌─ Compiler ─┐   ┌─ ReplayEngine ─┐
│ LLM: observe →   │──────────► │ fill in     │──►│ no LLM;        │──► result
│ decide → act     │            │ {{params}}, │   │ detectors,     │    (success |
└────────┬─────────┘            │ merge app   │   │ recoveries,    │     business outcome |
         │                      │ profile     │   │ checkpoint     │     failure)
         ▼                      └─────────────┘   └───────┬────────┘
   ┌──────────────────────── Surface ─────────────────────┘
   │ reads the page via the accessibility tree; acts via ranked locators
   │ SAFETY POLICY ENFORCED HERE — below the model, so it can't be talked around
   └── Escalation: pause → operator console drives the SAME live session → resume
```

The decisions I'd defend first:

- **Python, one process, synchronous.** The hard problems in this assignment are
  about data models and control flow, not scale. Queues and services would have
  been extra moving parts with nothing to justify them — the brief says as much.
- **The agent reads pages through the accessibility tree and targets elements by
  role and name** ("the textbox labeled Access Code"), not pixel coordinates or
  raw CSS. Three reasons: it works on messy legacy HTML with no test ids; it's
  basically how a human identifies controls, so it survives restyling; and the
  same concept exists on desktop apps, which matters for question 4 below.
- **Safety checks live below the model, in the Surface layer.** The model can
  only *propose* actions; the surface refuses anything outside the allowlist or
  matching a risky-button pattern. So even a prompt-injected model can't act
  outside policy — the same check runs in discovery and replay.
- **The model's output is just a trace of what it did — code builds the
  artifact.** A deterministic compiler does the parameterization, builds the
  fallback locators, and merges in the app profile. I didn't want the artifact's
  correctness to rest on "the model wrote it."
- **The discovery loop** (`cua/agent.py`) is Claude (`claude-opus-5`) with tool
  use: one action per turn, and a fresh page snapshot after every action. When
  the policy blocks something, the block is returned to the model as a tool
  error so it can try a different route.

## 2. Artifact schema

Defined in `cua/schema.py` (Pydantic, versioned, saved as JSON — real example in
`artifacts/`). I treated the artifact as a **contract for calling a capability**,
not just a list of steps:

- **Call signature**: typed `inputs` (with a `sensitive` flag) and typed
  `outputs` with instructions for where to read each value. A calling agent can
  see what to pass and what it gets back; `list` prints this as a catalog.
- **Steps with ranked locators**: for each target, the semantic locator the
  model actually used (role+name or label) comes first, then fallbacks captured
  at action time (visible text, `name=` attribute CSS, generated CSS path).
  Values are `{{param}}` templates — the artifact never contains real data.
- **A `row_cell` locator** for reading tables: "the cell in the row containing
  *Savings*, under the column *Current Balance*." Old bank UIs are tables
  without ids, and this survives rows moving around better than any CSS path.
- **Checkpoint**: a condition that must be visible before outputs are extracted.
  I never assume a click worked just because it didn't throw.
- **The error handling is data, not code** (details in §3): `outcome_detectors`,
  `recovery_rules`, and `hard_error_detectors` live in the artifact, each tagged
  with where it came from (`app_profile` or this capability).
- **Governance**: `status: draft → approved` — replay refuses drafts by default,
  so a human reviews what the model built before agents can call it. Plus a
  `risk` level (and `stops_before`: the irreversible button this flow must never
  press) and `provenance` (which model, which run, what goal). No transcript,
  no secrets.

## 3. Determinism & error handling

Replay (`cua/replay.py`) runs the steps with zero model involvement. What makes
it deterministic: locators are tried in rank order with a bounded wait (first
visible match wins), every action has an explicit wait after it, params are
bound by template substitution, and the only branching is data-driven (the
detectors below).

Since the UI itself is stable, the failures that matter are runtime conditions.
When a step fails — and also after every *successful* action, because a page can
answer the business question mid-flow — I check in this order:

1. **Hard-error detectors** (e.g. an "Application Error" page) → stop and return
   a debuggable failure: which step, what I expected, what I saw, plus a
   screenshot and an accessibility snapshot.
2. **Outcome detectors** → return a `business_outcome`. "No member found" is an
   answer the caller needs, not a crash — the result contract keeps these
   statuses separate, because mixing them up is the classic mistake here.
3. **Recovery rules** → small mechanical fixes with a `max_attempts` cap:
   dismiss a known popup, click "Restore Session", reload. The step retries and
   the result records that a recovery was applied.
4. Nothing matched → **escalate to a human** (if enabled) or fail hard with
   evidence.

If the final checkpoint doesn't appear, I re-scan the outcome detectors first —
a validation-error page after the last click is a business outcome, not a bug.
Slowness is absorbed by the waits (the evidence includes a run where an injected
6-second delay just shows up as a slower step). For UI drift, which the brief
calls secondary: replay records which locator strategy resolved each step, and
resolving via a fallback instead of the primary emits a `drift_signal` event —
that's the trigger to flag a capability for re-discovery.

## 4. Heterogeneity & multi-tenant

**Other kinds of surfaces.** The artifact and replay engine never mention
Playwright — they talk to a small `Surface` interface (observe the page, resolve
a target, act) using generic types. To support a desktop app, I'd write a new
implementation of that interface on Windows UIA or macOS Accessibility — and
role/name targeting already means something there, which is exactly why I made
it the primary strategy instead of CSS or pixels. For an app with no
accessibility layer at all, a screenshot+coordinates strategy would slot in as
one more locator kind, not a new engine.

**Reusing artifacts across tenants.** The schema already splits knowledge into
layers: what's specific to this capability vs. the **app profile**
(`profiles/legacy-teller.json`) — the error pages, recoveries, and outcome
signatures shared by *every* capability on the same vendor product, merged in at
compile time. The production version of this is a three-layer stack: vendor
product profile → tenant overlay (their URL, their renamed labels, their
version) → capability. That way hundreds of credit unions on the same core
banking product share one recorded flow, and a tenant who customized a screen
overrides only the affected targets. What exists today is the base of that:
`--base-url` per invocation, and semantic-first locators — which are the
locators most likely to survive per-tenant branding differences in the first
place. For detecting drift per tenant: aggregate the `drift_signal` events by
tenant and version, and you can see which tenants have diverged and need an
overlay or a re-record.

## 5. Escalation & handoff

**How "stuck" is detected.** In discovery: the model calls its `give_up` tool,
or runs out of steps. In replay: a step fails and no detector or recovery
matches, or the restart budget is used up.

**How control moves.** It's an explicit state machine (`cua/escalation.py`):
`AGENT_CONTROL → PAUSED → HUMAN_CONTROL → resume or abort`. On escalation the
run pauses and raises an intervention with real context: which capability, which
step, expected vs. observed, the URL, and a live screenshot. The operator
console (TypeScript, on `:7100`) shows that and lets the human drive **the same
live browser session** the automation was using — click and type commands are
executed against the live Playwright page. When they're done they hit Resume (or
Abort). Every human action is recorded from both channels: console commands, and
direct clicks in the browser window (an injected DOM listener reports those
while the human has control). The evidence log shows exactly what the human did.

**What resume means.** Resume restarts the capability from step 1 (one restart
by default). I chose restart-from-the-top because these capabilities don't
change anything up to their recorded boundary (`risk.stops_before`), so a
restart is always safe — and it avoids the genuinely hard problem of "which
half-finished step was I in?". For flows that *do* change things, resume would
need idempotency keys and per-step checkpoints; I designed for it but didn't
build it (§7). The console UI is deliberately minimal. The part I'd call real is
the mechanism — pause, expose the live session, record the human, resume,
restart — and in production you'd swap the localhost console for an
authenticated operator workspace and a remote browser pool behind the same API.

## 6. Safety

- **Allowlists are enforced below the model** (`policy/policy.json` →
  `cua/policy.py`, checked in the surface): allowed origins and allowed action
  types. Every navigation and every action is checked, identically in discovery
  and replay.
- **Risky / irreversible actions**: buttons matching configured patterns
  ("Confirm & Open", wire/transfer/delete/close-account…) are refused at click
  time. The demo flow stops at the review screen, and the artifact records
  `stops_before` so the boundary is explicit and reviewable. Default is block;
  there's an escalate mode that asks a human instead.
- **Secrets and PII**: sensitive values are **never sent to the model**. The
  model types `{{param}}` placeholders and the surface fills in the real value
  locally. Artifacts store placeholders only. Everything persisted (logs,
  results, snapshots) goes through a redactor seeded with the sensitive values
  plus configured patterns (PIN/SSN-like). Screenshots stay in local evidence
  folders and are what the operator sees during a handoff.
- **Honest limits**: screenshots and page snapshots can still contain on-screen
  PII (names, balances) — the redactor scrubs values it knows about, not
  arbitrary visual data. Production would need per-app field masking and an
  evidence retention policy. The allowlist is per-origin, not per-route. And
  draft→approved is one gate, not a full review workflow.

## 7. Cuts

Things I deliberately didn't build, with the boundary left clean so they slot in
later:

- **Non-Anthropic LLM support** — the model calls are isolated in `cua/agent.py`;
  a provider interface plus an OpenAI adapter is roughly a 100-line change.
- **Desktop / frameset surface implementations** — the `Surface` interface is
  the boundary (§4); I built one browser implementation.
- **Tenant overlay resolution** — designed in §4; today there's one profile
  layer plus `--base-url`.
- **Operator console hardening** — no auth, localhost only, minimal UI. The
  handoff mechanism and the action recording are real.
- **Resume-in-place for state-changing flows** — restart-only semantics today
  (§5); idempotency-keyed step checkpoints are the next step.
- **Assisted fallback** (letting the LLM fix a single failed step during replay,
  within policy) — the escalation hook is exactly where it would plug in. I cut
  it to keep "no model in production replay" a clean, true statement.
- **Multi-run stability scoring** — the per-step `strategy_used` and
  `drift_signal` data is already recorded; the job that aggregates it isn't
  built.
- **Iframe/frameset traversal** in locator resolution — would add a `frame_path`
  field to `ElementTarget`.

With more time, in order: tenant overlays with a drift dashboard, the
assisted-fallback experiment behind an approval flag, a desktop surface on UIA,
and a nightly harness that replays every artifact N times and scores stability
before agents are allowed to call it.
