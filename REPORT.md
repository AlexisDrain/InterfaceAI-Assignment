# Design Report

## 1. Architecture

The through-line: **the model discovers; the compiler produces a typed artifact; deterministic
replay is how agents invoke it in production.**

```
goal + typed params                    artifact + typed params
      │                                        │
      ▼                                        ▼
┌─ DiscoveryAgent ─┐   trace    ┌─ Compiler ─┐   ┌─ ReplayEngine ─┐
│ LLM observe→     │──────────► │ parameterize│──►│ no LLM;        │──► result contract
│ decide→act loop  │            │ + app profile│  │ detectors,     │    (success | business
└────────┬─────────┘            └─────────────┘   │ recoveries,    │     outcome | failure)
         │                                        │ checkpoint     │
         ▼                                        └───────┬────────┘
   ┌──────────────────────── Surface ─────────────────────┘
   │ perception: accessibility tree; action: ranked locator strategies
   │ POLICY ENFORCED HERE (allowlist, risky controls) — below the model
   └── Escalation: pause → operator console (TS) drives the SAME live session → resume
```

Key decisions:

- **Python, single process, synchronous.** The interesting problems here are data-model and
  control-flow problems, not scale problems. Queues/services would be premature (per the brief).
- **Perception = accessibility tree, action = semantic targeting** (role/name, label), not raw DOM
  or pixel coordinates. It works on non-semantic legacy markup, it's the one representation that
  also exists on desktops (UIA/AX APIs), and it's what a human operator effectively uses.
- **The policy layer lives in the surface, below the model.** The model proposes; the surface
  refuses anything outside the allowlist or matching risky-control patterns. A prompt-injected
  model still cannot act outside policy, in discovery or replay alike.
- **The LLM's output is a trace, not an artifact.** Deterministic compiler code — not the model —
  produces the artifact: parameterization, fallback-strategy construction, profile merging. This
  keeps the artifact trustworthy-by-construction rather than "the model said so."
- **Real LLM loop** (`cua/agent.py`): Claude (`claude-opus-5`) with tool use; one action per turn;
  fresh accessibility observation after every action; policy errors are surfaced back to the model
  as tool errors so it can adapt (e.g. pick a non-risky path).

## 2. Artifact schema

`cua/schema.py` (Pydantic, versioned, JSON-serialized; example in `artifacts/`). An artifact is a
capability contract, not just a step list:

- **Call signature**: typed `inputs` (with `sensitive` flags) and typed `outputs` with extraction
  targets. A calling agent can read what to pass and what it gets back; `list` renders the catalog.
- **Steps** with **ranked locator strategies** per target: the semantic strategy the model used
  first (role+name / label), then structural fallbacks captured at action time (visible text,
  `name=` attribute CSS, generated CSS path). Values are `{{param}}` templates.
- **`row_cell` locator**: table cells addressed as (row text × column header) — built for
  legacy table-soup where cells have no ids; survives row reordering and column insertion better
  than any CSS path.
- **Checkpoint**: explicit success conditions, verified before outputs are extracted — never
  "the click didn't throw, so it worked."
- **Error taxonomy as data** (see §3): `outcome_detectors`, `recovery_rules`,
  `hard_error_detectors`, each tagged with `origin: app_profile | capability`.
- **Governance**: `status: draft → approved` (replay refuses drafts by default — a human reviews
  what the model built before agents may invoke it), `risk` (level + `stops_before`, the
  irreversible control the flow must never touch), and `provenance` (model, run id, goal) — while
  containing nothing of the raw transcript and no secrets.

## 3. Determinism & error handling

Replay (`cua/replay.py`) executes steps with zero model involvement. Determinism comes from:
ranked locator resolution with a bounded poll (first visible match wins), explicit post-action
waits, template binding of params, and data-driven branching only.

Because the UI is stable, the interesting failures are runtime conditions. Triage order on any
step failure — and the same scan runs after every successful action, since a page can answer the
business question mid-flow:

1. **Hard-error detectors** (e.g. "Application Error" page) → stop with a debuggable
   `FailureDetail`: step, expected, observed, plus screenshot + accessibility snapshot evidence.
2. **Outcome detectors** → return `business_outcome` (e.g. `member_not_found`,
   `input_rejected`). "No such member" is an answer for the caller, not a crash — the result
   contract separates these statuses explicitly.
3. **Recovery rules** → bounded, mechanical fixes (dismiss a known interstitial, click "Restore
   Session", reload), with `max_attempts`; the step is retried and marked `recovered`.
4. Otherwise → **escalate** (if enabled) or fail hard with evidence.

A checkpoint miss re-scans outcomes first: a validation-error page after the final click is a
business outcome, not a failure. Transient slowness is absorbed by waits (the evidence includes a
run where the injected 6s delay simply shows up as an 8.5s step). **Drift, secondarily**: replay
records which strategy resolved each step; resolving via a fallback instead of the primary
emits a `drift_signal` event — the trigger for flagging a capability for re-discovery.

## 4. Heterogeneity & multi-tenant

**Surface seam.** Artifacts and the replay engine never touch Playwright types; they speak
`ElementTarget`/`Observation` to a `Surface` interface (perceive, resolve, act). A desktop
implementation (Windows UIA / macOS AX) drops in behind the same interface — role/name targeting
and accessibility-tree perception are natively meaningful there, which is exactly why pixel
coordinates and CSS-first targeting were rejected as the primary strategies. A screenshot+
coordinates surface would be the fallback of last resort for apps with no accessibility layer,
implemented as one more strategy kind, not a new engine.

**Multi-tenant reuse.** The schema already splits knowledge into layers:
capability-specific data vs. the **app profile** (`profiles/legacy-teller.json`) — outcome
detectors, recoveries, and error signatures shared by *every* capability on the same vendor
product, merged at compile time and tagged with `origin`. The production shape is a three-layer
resolution — vendor product profile → tenant overlay (base_url, branding-affected names, version
pins) → capability — so hundreds of tenants on the same core banking product share one recorded
flow, and a tenant with a customized screen overrides only the affected targets. Two mechanisms
exist today as the seam: `--base-url` per invocation (tenant instance), and semantic-first
locators, which are exactly the strategies most stable across branding/config differences.
Per-tenant drift management: the `drift_signal` telemetry above, aggregated per tenant+version,
tells you which tenants have diverged and need an overlay or a re-discovery.

## 5. Escalation & handoff

**Detecting stuck**: discovery — the model calls `give_up`, or the step budget runs out; replay —
a step fails and no detector/recovery matches, or the restart budget is exhausted.

**Control transfer** is an explicit state machine (`cua/escalation.py`):
`AGENT_CONTROL → PAUSED → HUMAN_CONTROL → (resume | abort)`. On escalation the run pauses and an
intervention is raised carrying full context: capability, step, expected-vs-observed, URL, live
screenshot. The operator console (TypeScript, served at `:7100`) shows that context and drives
**the same live browser session** the automation was using — structured click/type commands
executed against the live Playwright page — then hands control back with Resume, or Aborts.
Every operator action is recorded, from both channels: console commands *and* direct browser
interaction (an injected DOM listener reports clicks/changes while a human has control), so the
evidence log shows exactly what the human did.

**Resume semantics**: resume restarts the capability from step 1 (one restart by default).
Rationale: capabilities are non-mutating up to their recorded boundary (`risk.stops_before`), so
restart is always safe and avoids the hairy "which half-finished step state am I in?" problem.
For genuinely mutating capabilities, resume would need idempotency keys/checkpointed step state —
designed but deliberately not built (§7). The console UI is minimal by intent; the *mechanism*
(pause, expose live session, record human actions, signal resume, restart) is real and is the
production seam — a real deployment swaps the localhost console for an authenticated operator
workspace and a remote browser pool (CDP/VNC) behind the same API.

## 6. Safety

- **Allowlists enforced below the model** (`policy/policy.json` → `cua/policy.py`, enforced in the
  surface): origins and action types. Every navigation and every action is checked, in discovery
  and replay identically.
- **Risky/irreversible actions**: controls matching configured patterns ("Confirm & Open",
  wire/transfer/delete/close-account…) are refused at click time. The demo flow stops at the
  review screen; the artifact records `stops_before` as an explicit, reviewable boundary.
  Conservative default (`block`); `escalate` mode routes the decision to a human instead.
- **Secrets & PII**: sensitive parameter values are **never sent to the model** — the model types
  `{{param}}` placeholders and the surface substitutes values locally at fill time. Artifacts
  store placeholders only. All persisted text (logs, results, snapshots) passes through a
  redactor seeded with sensitive values plus configured patterns (PIN/SSN-like). Screenshots stay
  in local evidence directories and are the operator's window during handoff.
- **Limits, honestly**: screenshots and accessibility snapshots can still contain on-screen PII
  (balances, names) — production would need field-level masking policies per app profile and an
  evidence retention policy; the redactor handles values it knows, not arbitrary visual data.
  The allowlist is origin-granular, not route-granular. Draft→approved is one gate, not a full
  review workflow.

## 7. Cuts

Deliberately cut, with the seam left clean:

- **Non-Anthropic LLM** — provider-agnostic LLMProvider interface out of scope. Could
  be added with a ~100-line adapter.
- **Desktop/legacy-frameset surface implementations** — the `Surface` interface and
  accessibility-first targeting are the seam (§4); only one browser implementation is built.
- **Tenant overlay resolution** — designed (§4); today one profile layer + `--base-url` exists.
- **Operator console hardening** — no auth, localhost only, minimal UI; the handoff mechanism and
  recording are real.
- **Resume-in-place for mutating flows** — restart semantics only (§5); idempotency-keyed step
  checkpointing is the next step for `makes_change` capabilities.
- **Assisted fallback** (bounded single-step LLM recovery on replay failure) — the escalation hook
  is exactly where it would plug in; cut to keep the "no model in production replay" story crisp.
- **Multi-run stability scoring** — the per-step `strategy_used`/`drift_signal` telemetry is
  recorded; the aggregation job isn't built.
- **Frames/iframe traversal** in locator resolution; would add a `frame_path` to `ElementTarget`.

Next with more time: tenant overlays + drift dashboards, the assisted-fallback experiment behind
an approval flag, desktop surface via UIA, and eval harnesses that replay every artifact N times
nightly and score stability before agents are allowed to call it.
