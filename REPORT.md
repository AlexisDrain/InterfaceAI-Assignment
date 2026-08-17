# Design Report

## 1. Architecture

The through-line: **the model discovers; the compiler produces a typed artifact; deterministic
replay is how agents invoke it in production.**

```
goal + typed params                            artifact + typed params
      │                                                 │
      ▼                                                 ▼
┌─ DiscoveryAgent ─┐   trace    ┌── Compiler ──┐   ┌─ ReplayEngine ─┐
│ LLM observe→     │──────────► │ parameterize │──►│ no LLM;        │──► result contract
│ decide→act loop  │            │ + app profile│   │ detectors,     │    (success | business
└────────┬─────────┘            └──────────────┘   │ recoveries,    │     outcome | failure)
         │                                         │ checkpoint     │
         │                                         └───────┬────────┘
         ▼                                                 ▼
   ┌──────────────────────── Surface ───────────────────────────────────┐
   │ perception: accessibility tree; action: ranked locator strategies  │
   │ POLICY ENFORCED HERE (allowlist, risky controls) — below the model │
   └───────────────────────────┬────────────────────────────────────────┘
                  stuck: pause ▼▲ resume / abort
   ┌─ Escalation (InterventionManager) ────────────────────┐
   │ a human drives the SAME live session — console        │
   │ commands or direct browser clicks, all recorded       │
   └───────────────────────────────────────────────────────┘
```

Key decisions:

- **Python, single process, synchronous.** The interesting problems are data-model and
  control-flow problems, not scale problems; queues/services would be premature.
- **Perception = accessibility tree, action = semantic targeting** (role/name, label), not raw
  DOM or pixels. It works on non-semantic legacy markup, it's the one representation that also
  exists on desktops (UIA/AX), and it's what a human operator effectively uses.
- **Policy lives in the surface, below the model.** The model proposes; the surface refuses
  anything outside the allowlist or matching risky-control patterns — so a prompt-injected
  model still cannot act outside policy, in discovery or replay alike.
- **The LLM's output is a trace, not an artifact.** Deterministic compiler code produces the
  artifact (parameterization, fallback strategies, profile merging) — trustworthy by
  construction rather than "the model said so."
- **Real LLM loop** (`cua/agent.py`): Claude with tool use; one action per turn; fresh
  accessibility observation after every action; policy refusals surface back to the model as
  tool errors so it can adapt.

## 2. Artifact schema

`cua/schema.py` (Pydantic, versioned, JSON; example in `artifacts/`). An artifact is a
capability contract, not just a step list:

- **Call signature**: typed `inputs` (with `sensitive` flags) and typed `outputs` with
  extraction targets; `list` renders the catalog a calling agent reads.
- **Steps with ranked locator strategies**: the semantic strategy the model used (role+name /
  label) first, then structural fallbacks captured at action time (visible text, `name=` CSS,
  generated CSS path). Values are `{{param}}` templates.
- **`row_cell` locator**: table cells addressed as (row text × column header) — built for
  legacy table-soup with no ids; survives reordering better than any CSS path.
- **Checkpoint**: explicit success conditions verified before outputs are extracted — never
  "the click didn't throw, so it worked."
- **Error taxonomy as data** (§3): `outcome_detectors`, `recovery_rules`,
  `hard_error_detectors`, each tagged `origin: app_profile | capability`.
- **Governance**: `status: draft → approved` (replay refuses drafts — a human reviews what the
  model built), `risk` (level + `stops_before`, the irreversible control the flow must never
  touch), `provenance` (model, run id, goal). No transcript, no secrets.

## 3. Determinism & error handling

Replay (`cua/replay.py`) executes with zero model involvement: ranked locator resolution with
a bounded poll, explicit post-action waits, template binding, data-driven branching only.

The interesting failures are runtime conditions. Triage on any step failure — and the same
scan after every successful action, since a page can answer the business question mid-flow:

1. **Hard-error detectors** (e.g. "Application Error") → stop with a debuggable
   `FailureDetail`: step, expected, observed, screenshot + accessibility snapshot.
2. **Outcome detectors** → return `business_outcome` (`member_not_found`, `input_rejected`).
   "No such member" is an answer for the caller, not a crash; the result contract keeps these
   statuses separate.
3. **Recovery rules** → bounded mechanical fixes (dismiss a known interstitial, "Restore
   Session", reload) with `max_attempts`; the step retries and is marked `recovered`.
4. Otherwise → **escalate** (if enabled) or fail hard with evidence.

A checkpoint miss re-scans outcomes first: a validation error after the final click is a
business outcome, not a failure. Transient slowness is absorbed by waits. **Drift,
secondarily**: replay records which strategy resolved each step; resolving via a fallback
emits a `drift_signal` event — the trigger for flagging a capability for re-discovery.

## 4. Heterogeneity & multi-tenant

**Surface seam.** Artifacts and replay never touch Playwright types; they speak
`ElementTarget`/`Observation` to a `Surface` interface (perceive, resolve, act). A desktop
implementation (Windows UIA / macOS AX) drops in behind the same interface — role/name
targeting is natively meaningful there, which is exactly why pixels and CSS-first targeting
were rejected as primaries. A screenshot+coordinates surface would be one more strategy kind
for apps with no accessibility layer, not a new engine.

**Multi-tenant reuse.** The schema already splits knowledge into layers: capability data vs.
the **app profile** (`profiles/legacy-teller.json`) — detectors, recoveries, and error
signatures shared by every capability on the same vendor product, merged at compile time and
tagged with `origin`. The production shape is three layers — vendor profile → tenant overlay
(base_url, branding-affected names, version pins) → capability — so hundreds of tenants on
the same core product share one recorded flow and a customized tenant overrides only the
affected targets. Today the seam is `--base-url` per invocation plus semantic-first locators
(the strategies most stable across branding differences). Aggregating `drift_signal` per
tenant+version shows which tenants diverged and need an overlay or re-discovery.

## 5. Escalation & handoff

**Detecting stuck**: discovery — the model calls `give_up`, or the step budget runs out;
replay — a step fails with no detector/recovery match, or the restart budget is exhausted.

**Control transfer** is an explicit state machine (`cua/escalation.py`):
`AGENT_CONTROL → PAUSED → HUMAN_CONTROL → (resume | abort)`. An intervention carries full
context: capability, step, expected-vs-observed, URL, live screenshot. The operator console
(TypeScript, `:7100`) drives **the same live browser session** the automation was using, then
hands back with Resume. Every human action is recorded from both channels — console commands
and direct browser interaction (an injected DOM listener) — so the evidence shows exactly
what the human did.

**Resume semantics**: resume restarts the capability from step 1 (one restart by default) —
safe because capabilities are non-mutating up to `risk.stops_before`, and it avoids the
"which half-finished step am I in?" problem. Mutating capabilities would need idempotency
keys / checkpointed state — designed, not built (§7). The console UI is minimal by intent;
the mechanism (pause, expose live session, record, resume) is the production seam — a real
deployment swaps in an authenticated workspace and a remote browser pool behind the same API.

## 6. Safety

- **Allowlists enforced below the model** (`policy/policy.json` → surface): origins and
  action types, checked on every action in discovery and replay identically.
- **Risky/irreversible actions**: controls matching configured patterns ("Confirm & Open",
  wire/transfer/delete…) are refused at click time; the artifact records `stops_before` as an
  explicit boundary. Default `block`; `escalate` mode routes the decision to a human.
- **Secrets & PII**: sensitive values are **never sent to the model** — it types `{{param}}`
  placeholders and the surface substitutes locally. Artifacts store placeholders only; all
  persisted text passes through a redactor seeded with sensitive values + configured patterns.
- **Limits, honestly**: screenshots/snapshots can still contain on-screen PII — production
  needs field-level masking and retention policies; the redactor handles values it knows. The
  allowlist is origin-granular, not route-granular. Draft→approved is one gate, not a full
  review workflow.

## 7. Cuts

Deliberately cut, seam left clean:
- **non-Anthropic LLM support** (isolated in `cua/agent.py`;
~100-line adapter);
- **desktop/frameset surface implementations** (the `Surface` interface is
the seam);
- **tenant overlay resolution** (designed in section 4; one profile layer + `--base-url`
exists);
- **operator console hardening** (no auth, localhost, minimal UI — the handoff and
recording are real);
- **resume-in-place for mutating flows** (restart-only right now);
- **assisted fallback** (bounded single-step LLM recovery — the escalation hook is where it
plugs in; cut to keep "no model in production replay" crisp);
- **multi-run stability scoring** (telemetry recorded, aggregation not built);
- **iframe traversal** (would add `frame_path`).

Next with more time: tenant overlays + drift dashboards, assisted fallback behind an approval
flag, a UIA desktop surface, and nightly harnesses that replay every artifact N times and
score stability before agents may call it.
