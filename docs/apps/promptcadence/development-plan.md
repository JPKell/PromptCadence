# PromptCadence — Development Plan

**Sequence position:** PromptCadence arc, stream S ([roadmap §4](../../roadmap/promptcadence-roadmap.md)).
P1–P2 need only `baseaicore>=0.4.1` and the published foundation packages; P4–P8 consume the four
new packages as each publishes 0.1.0.
**Target:** `promptcadence 0.9-beta` at the end of P7 (M11), `promptcadence 1.0.0` at the end of P9 (M12).

The build order enforces two principles. First, **governance grows inward-out**: the bypass loop —
the simplest thing that executes a turn — is built first, and every subsequent phase adds a
governance layer to *both* paths at once, so contract 1 (governance invariance,
[spec §11](spec.md)) is true at every commit rather than reconciled at the end. Second, **nothing
harmful before its discipline**: no tool executes before ToolYard's refusal machinery is
integrated, no remote tier is configurable before egress and pricing enforcement exist.

---

## Phase 1 — Skeleton, configuration, database, health

**Goal:** `promptcadence serve` starts, migrates, reports health honestly with nothing else running.

**Prerequisites:** Phase 0 of the [roadmap](../../roadmap/promptcadence-roadmap.md) (ADRs accepted,
docs merged); `baseaicore 0.4.1` published.

**Work**
* Repository per the standard application layout (master architecture §4); standard toolchain;
  `.importlinter` contracts (web/cli → services → domain; domain framework-free; no application
  imports).
* `config.py`: the full spec §12 surface with precedence, startup validation (remote tier rules,
  classification values, binding-vs-auth refusal, manual-mode token check).
* WeightsDB wiring; Alembic history; core tables: `trajectories`, `threads`, `turns`, `events`,
  `api_tokens`, `settings`.
* MirrorWall base: envelopes, request IDs, SSE helper, telemetry widget (fed from LoadCoach
  `/system/status` when reachable).
* `GET /health`, `/version`, `/system/status` (degraded components honest from day one);
  `promptcadence serve|health|version|config|db`; `doctor` skeleton.

**Tests**
* Config precedence field-by-field; every startup refusal with its message.
* Migration up/down on both dialects; backup/restore.
* Health: no LoadCoach ⇒ degraded `loadcoach` component, HTTP still 200.

**Acceptance criteria**
1. `promptcadence serve` on a clean machine with zero configuration; health degraded, not dead.
2. Gate clean (`ruff`, `mypy --strict`, `lint-imports`, pytest); CI green.

**Known risks:** none novel — this phase is deliberately identical in shape to LoadCoach P1.
**Gold standards:** zero-config start; honest degradation.
**Deferred:** everything that executes.

---

## Phase 2 — Domain core: threads, tiers, classification, plans, state machine

**Goal:** every governance decision that needs no I/O is a pure, golden-tested function.

**Prerequisites:** Phase 1.

**Work**
* `domain/threads.py`: `Thread`, `Turn`, `ThreadSnapshot`, `ThreadStore` protocol — **built
  package-shaped** (no PromptCadence vocabulary in the types), per the recorded ThreadRack
  rejection ([spec §10](spec.md)). `Turn` is generic over its provenance, which is both how a
  host attaches its own vocabulary without contaminating the type and how "no turn without an
  intent" becomes structural. `infrastructure/threads.py`: `SqlThreadStore` — **not** in
  `domain/`, which the `domain-purity` import contract forbids `sqlalchemy` inside; the earlier
  wording here contradicted that contract and the contract wins.
* `domain/tiers.py`: `Tier`, `TierPolicy` — admission (`classification ≤ ceiling`), default tier,
  escalation order, tier snapshots for trajectories (content-addressed; see [spec §10](spec.md)).
  The domain `Tier` is a frozen dataclass built from `config.Tier` at the boundary by
  `services/policy_assembly.py`, so the domain stays free of a validation framework's semantics
  and `TierPolicy` is testable without constructing a `Settings`.
* `domain/plan.py`: `Plan`, `PlanStep`, the plan JSON Schema (committed + golden), validation
  (DAG, tools, tiers, classification laundering, non-empty).
* `domain/intent.py`: `ExecutionIntent` ([lifecycle §4.3](lifecycle.md)) — immutable, revisioned;
  minting from approved plan verdicts (redlines resolved at minting), from bypass defaults, and by
  supersession; `domain/deviation.py`: the pure `compare(turn_facts, intent)` over the closed
  category set ([lifecycle §5](lifecycle.md)).
* `domain/trajectory.py`: the state machine exactly as the [lifecycle §8.1–8.3](lifecycle.md)
  tables define it — every T-row an explicit function, every illegal transition refused;
  `domain/policy.py`: approval-policy evaluation (auto verdicts) as a pure function over tier
  policy + ledger verdicts — human modes arrive in P7.
* Event types and payload shapes matching spec §17: `domain/events.py` owns the closed
  `EventType` vocabulary and the "ids, categories and numbers — never prompt text, never model
  output" rule, and each event body lives with the code that mints it. Naming the module
  `observability` would have `domain` importing an infrastructure package to name its own events;
  writing them is Phase 3's, and that is what `observability` owns.

**Tests**
* Golden plan validations (valid, cyclic, laundering, empty, unknown tool/tier).
* Intent minting goldens (plan, redline, bypass default, superseding revision); immutability
  asserted; gate evaluation against the most permissive tier in `fallback_tiers`.
* The deviation matrix: every category × severity × `reapproval_scope` cell, parametrized over
  the enums so the matrix is exhaustive by construction.
* Tier admission matrix; escalation ordering; snapshot immutability.
* State machine: every lifecycle §8.2 row, every illegal transition refused; property test that
  terminal states have no exits.

* Migration `0002`: `tier_snapshots`, `plans`, `plan_steps`, `plan_approvals`,
  `approval_requests`, `execution_intents` and `deviations`, plus `turns.intent_id`,
  `turns.intent_revision`, `turns.cache_write_tokens`, `turns.cache_read_tokens` and
  `trajectories.tier_snapshot_id` / `.approval_policy_version`. Born with the domain types that
  define them rather than retrofitted by the loop that first writes them — P3's first act mints
  an intent at claim (T3), and every turn persists `(intent_id, revision)`.

**Acceptance criteria**
1. Domain modules import no framework (asserted by import-linter).
2. Determinism goldens for plan validation and approval evaluation.

**Known risks:** the plan schema fitting the planner model poorly (too strict ⇒ constant retries).
Mitigated by P7's corrective-retry budget and by validating the schema against real local-model
output during P7, before beta.
**Gold standards:** pure domain; docstring-first throughout.
**Deferred:** anything that talks HTTP.

---

## Phase 3 — LoadCoach client, bypass loop, events and recovery

**Goal:** a bypassed trajectory executes end to end — no tools, no budget yet — durably and
observably.

**Prerequisites:** Phase 2.

**Work**
* `infrastructure/loadcoach.py`: the httpx client — `/generate`, `/route`, `/task-profiles`,
  `/system/status`; error mapping per spec §13; timeouts; auth header from env/file.
* **The fake LoadCoach** (`tests/fakes/loadcoach_app.py`): an in-process ASGI app speaking
  LoadCoach's documented response shapes, scriptable per test (results, tool_calls, errors,
  slowness) — the FakeProvider lesson applied one layer up: built *before* the loop, so every
  downstream phase tests without a GPU or a live LoadCoach.
* `services/loop.py`: `LoopController` bypass path — claim (lease) → mint the default
  `ExecutionIntent` (T3) → turn via `TierRouter` under that intent → append → events → finish on
  declared `finish_reason` / `max_turns`; `BypassGate` reading config + request override.
* Trajectory worker thread with lease renewal and startup recovery
  ([ADR-0036](../../adr/0036-queue-recovery-transitions.md) edges); cancellation at turn
  boundaries, propagated to the in-flight LoadCoach job.
* `POST /trajectories`, `GET /trajectories(/{id})`, `/cancel`, the SSE stream with replay;
  `promptcadence run|trajectory list|show|cancel|wait`.

**Tests**
* Full bypass journey against the fake; every LoadCoach error row mapped; finish-reason handling
  (`LENGTH`/`ERROR` never read as success).
* Kill −9 mid-turn: recovery resumes or halts per lease; no duplicate turn, no orphaned job
  (cancel issued for unreconcilable in-flight work).
* SSE replay from `Last-Event-ID`; state change + event are one write
  ([ADR-0044](../../adr/0044-a-state-change-and-its-event-are-one-write.md), asserted with a
  crash-between test).

**Acceptance criteria**
1. `promptcadence run "…" --bypass-planning` completes against the fake and against a real local
   LoadCoach (marked live test).
2. Recovery criteria (spec §20 #9, minus tools) pass.

**Known risks:** the fake drifting from real LoadCoach. Mitigated by contract tests against
LoadCoach's committed OpenAPI snapshot (I10) and one marked live journey per phase from here on.
The snapshot types every LoadCoach *response* as an open object, so it pins request bodies and
paths; response shapes are pinned by transcription from `api.md` §4 and asserted key-for-key in
the client tests.
**Gold standards:** durable queue discipline; explicit finish-reason contract.
**Deferred:** tools, money, egress, planning.
**As built (D2):** the client is `infrastructure/loadcoach.py`; the subject's egress class is
resolved in `services/loadcoach_surface.py` from `/models` (LoadCoach's `/system/status` carries
no provider information); `TierRouter` and `BypassGate` live in `services/loop.py`; the worker,
lease keeper and recovery pass in `services/worker.py`; the views shared by the service and the
loop in `services/views.py`; the ADR-0044 sink in `services/events.py`; migration `0003` adds the
lease, cancel and error-code columns to `trajectories`. LoadCoach `01170a7` rendered no
`finish_reason` (spec §11 contract 6), so on that wire a free-text tier halted on its first turn
and only a schema-validating profile completed; LoadCoach `846348b` renders the
declared reason at `output.finish_reason` and the job document's validation `checks`, the fake
speaks that wire, a free-text tier completes on a declared `stop`, and a `length` finish, an
undeclared reason or an absent field halt with the cause on the row. The live half of
acceptance criterion 1 passes against a LoadCoach at or after that commit and fails, naming the
gap, against an older one.

---

## Phase 4 — Tools

**Goal:** the loop executes tool calls under full ToolYard discipline.

**Prerequisites:** Phase 3; `toolyard 0.1.0`.

**Work**
* Registry assembly from `[tools]` config; per-trajectory workspaces under `workspace_root`;
  per-trajectory allowlists (request ⊆ config).
* Tool round trips inside a step: `tool_calls` → ToolYard → results appended as TOOL turns →
  continue until declared finish; `max_turns_per_step`.
* The tool-call store; `tool_call_records` table; `tool.call.*` events; oversize outputs to the
  artifact directory by hash. **Built as `CollectingToolCallStore`, not `SqlToolCallStore`**: the
  executor appends its record from inside `execute()`, and a `run_command` may spend its whole
  timeout inside a container — so a store that wrote through would hold a SQLite write lock for
  exactly that long. It collects during the call and is flushed onto the session the turn commits
  on, which keeps ADR-0044's one-write property (record, `TOOL` turn and `tool.call.completed`
  are atomic with each other) without a transaction open across a subprocess.
* `GET /tools`, `promptcadence tools list|show`.

**Tests**
* The spec §18 security rows that exist at this layer: unlisted tool, path escape, symlink
  escape, refusal fed back as a structured TOOL turn (trajectory continues), size caps.
* A scripted multi-tool journey against the fake; the marked live journey now includes one real
  `read_file`.

**Acceptance criteria**
1. A hostile scripted model (requests unlisted tools, escaping paths, huge outputs) completes or
   halts cleanly with every call recorded — no exception ever crosses the loop.

**Known risks:** workspace lifecycle (cleanup vs retention). Decided here: workspaces follow
content retention — swept with transcript text, hashes kept.
**Gold standards:** no unvalidated model argument reaches a side effect.
**Deferred:** network egress for tools remains off until P6 (`http_fetch` requires egress
governance in place). **Decided at P4:** `http_fetch` stays in `[tools] enabled`'s shipped default
and is **withheld from the registry** with a named cause, rather than removed from the default.
Spec §12's documented configuration keeps working for an operator who copied it, P6 flips one guard
instead of editing a shipped list, and the tool's absence is *visible* — `GET /tools`,
`promptcadence tools list` and `doctor` each name it with the cause. A model that asks for it is
refused with `unknown_tool`, which is true: it is not registered. Two independent facts keep it off
the network — it is not in the registry, and every invocation's egress ceiling is `none`.

---

## Phase 5 — Budget

**Goal:** both ceilings bind, estimates are labelled, and the ledger survives crashes.

**Prerequisites:** Phase 4; `loadledger 0.1.0`.

**Work**
* Mount `loadledger.sql` into PromptCadence's metadata + Alembic history; configure ceilings from
  `[budget]` and per-request overrides.
* Debit per turn (LoadCoach usage) and per summarization-to-come; `would_exceed` pre-flight per
  turn; exhaustion → halt or `awaiting_approval` per config; `budget.debited` events.
* `[budget] partial_pricing` (per-request override like the ceilings) onto every money ceiling; a
  floor renders as "at least" in API and CLI; a strict ceiling's pre-flight refusal (ADR-0069).
* `declare_run` at trajectory creation, before plan approval, so no pre-flight check ever meets
  `UnknownRun`; the debit rebuilds `TokenUsage` from all four classes on LoadCoach's job document
  (ADR-0070, row C6).
* The `project` request label: refused unless configured (`PROJECT_UNKNOWN`); `project:<name>` on
  every debit beside `tier:<name>`; a per-tag money/token ceiling per `[budget.projects.<name>]`,
  resolved with the trajectory's own and the per-day ceiling into the ledger view per operation
  (LoadLedger plan Phase 2); per-project position on `GET /ledger` and the dashboard.
* Exhaustion per ceiling: `on_exhausted` (approval | halt) for the per-trajectory and per-project
  ceilings; `on_daily_exhausted` (window | approval | halt) for the per-day ceiling, with the
  `awaiting_window` park and resume (lifecycle §8 T15–T17) and `window_wait_max_days`.
* The historical estimator ([lifecycle §6](lifecycle.md)) over `entries()`, with source labels;
  per-tier configured defaults.
* `GET /ledger(/entries)`, `promptcadence ledger show`.

**Tests**
* Crossing each ceiling mid-trajectory; token ceiling binding a local tier where money cannot
  (the ADR-0030 case); daily UTC window; estimator source selection at the sample threshold.
* A response the provider did not fully price: under `floor` the trajectory continues and the
  balance shows "at least"; under `strict` the next step is refused at pre-flight; a local step
  trips neither.
* A trajectory parked on the per-day ceiling resumes when the injected clock crosses UTC midnight
  and the ceiling admits it; stays parked when another trajectory has already spent the new day;
  halts after `window_wait_max_days`. A project ceiling binds across two trajectories that share
  the label. An unknown project is refused before anything is persisted.
* Kill −9 between LoadCoach response and debit: recovery reconciles from the persisted turn —
  spend is never lost or double-debited.

**Acceptance criteria**
1. Spec §20 #6 passes; unpriced local usage shows `—`, never `$0.00`, in API, CLI and (later) UI.

**Known risks:** reconciling usage for a turn whose debit crashed. The turn row is the source of
truth; recovery re-derives the debit from it (idempotent by `source_ref`).
**Gold standards:** store usage, derive cost; exact arithmetic end to end.
**Deferred:** approval-gated ceiling raises (P7).

---

## Phase 6 — Egress, verification and deviation

**Goal:** the classification lattice governs every turn; drift is detected and handled.

**Prerequisites:** Phase 5; `commissioner 0.1.0` (which needs `setspec 0.5.0`).

**Work**
* Mount `commissioner.sql`; evaluate before every turn (tier target) and every `NETWORK` tool call;
  record every verdict; denial ends the turn with a structured refusal per spec §13.
* Post-turn verification: response execution subject vs tier promise; mismatch → `VIOLATION`
  decision + halt (spec contract 4).
* `DeviationHandler` wired end to end against the **bypass default intent**: the full
  [lifecycle §5](lifecycle.md) category set applies unchanged (the intent is the comparison
  source in both modes — P7 only changes who mints it); deviation events carrying categories;
  halt thresholds.
* Enable `http_fetch` (egress-checked); `GET /egress-decisions`, `promptcadence egress list`.
* Remote tiers become *configurable* now — and refuse with `TIER_UNAVAILABLE /
  loadcoach_has_no_remote_provider` or `UNPRICED_EGRESS_REFUSED` as documented, which is itself a
  tested behaviour.

**Tests**
* Spec §20 #4 and #5 verbatim; the fake plays a remote provider answering a local tier →
  violation + halt; fetch to a non-allowlisted host refused and recorded.
* Deviation matrix (bypass rows) golden.

**Acceptance criteria**
1. Every turn in every prior phase's journeys now carries an egress decision — the invariance
   check runs from here to 1.0.

**Known risks:** verification false positives if LoadCoach metadata is incomplete. The response
names its execution subject by contract ([LoadCoach spec §9](../loadcoach/spec.md)); absence is
treated as a violation, not a pass — fail closed, recorded.
**Gold standards:** fail closed; denial as auditable as approval.
**Deferred:** plan-declared deviation rows (P7).

---

## Phase 7 — Planning and approval — **beta (M11)**

**Goal:** the full planned path: draft, approve (all three modes), execute per DAG, re-approve on
drift.

**Prerequisites:** Phase 6.

**Work**
* `Planner`: LoadCoach call under `tools.plan` (`response_format="json"`), PromptCadence-side schema
  validation, bounded corrective retry — the ADR-0041 pattern; planner prompt as a versioned
  record.
* `PlanApprover` wired to modes: auto (P2's pure evaluation), hybrid gates, manual;
  `approval_requests`, timeouts, the `approve` scope; approval's output is intent minting (T4/T8),
  and scoped re-approval mints superseding revisions; `POST /approve|/deny`, `GET /approvals`,
  `promptcadence approvals|approve|deny`.
* `LoopController` planned path: ready-set DAG dispatch (`max_concurrent_steps`, disjoint-surface
  rule); per-step intents driving `TierRouter`; the `DeviationHandler` unchanged from P6 — only
  the minting source is new.
* Task-profile checks in `doctor` and `promptcadence tiers check`; the shipped profile TOML documented.

**Tests**
* Planned journeys: auto-approved; hybrid pausing at the gated step; manual deny; approval
  timeout; redlined substitution; scoped re-approval on an unplanned tool; DAG with a parallel
  local+remote pair under the concurrency rule (fake).
* **The contract-1 diff**: planned vs bypassed record shapes identical minus plan rows —
  the load-bearing test of the whole design.
* Plan-schema resilience against real local-model output (marked live, small model).

**Acceptance criteria**
1. Spec §20 #2, #3, #7, #8 pass; the M11 exit condition
   ([roadmap §3](../../roadmap/promptcadence-roadmap.md)) is demonstrated end to end on real
   LoadCoach + Ollama.
2. Tag `0.9-beta` (`0.9.0b0`), cut at the demonstration, not before.

**Known risks:** local models drafting unusable plans. The corrective budget, the `tools.plan`
profile's constraints and — if needed — a simpler fallback plan shape (single linear step list)
are the levers; a persistent failure here is finding-grade input to the future `native.plan`
benchmark, not something to paper over.
**Gold standards:** the model never decides control flow; approval before execution.
**Deferred:** compaction, UI, hardening.

**Built (G1, 2026-09-04; `docs/history/G1_HANDOFF.md`).** Every work item above, in six gates: the planner
and its prompt pack; approval in three modes on both paths, the bypass gate wired (a `manual`
or gated `hybrid` bypass parks before any turn, and the grant supersedes the default intent);
scoped re-approval, tier escalation and the ceiling raise as real T10 requests; ready-set DAG
dispatch under the disjoint-surface rule; the contract-1 diff with its named allowance list;
`approvals list|approve|deny`, `tiers list|show|check`, `token create|list|revoke`,
`GET /approvals`, `/plan`, `/intents`, the `tiers` health component and a real
`GET /system/status`. Two things the plan's wording implied are **not** in this version and are
recorded rather than approximated: a per-step *retry/wait* policy (spec §13 — no §12 key existed
for one; the cells halted with the cause. Built at row G3 on 2026-09-05 as `[execution]
step_retries` under [ADR-0076](../../adr/0076-a-step-retry-is-a-repeat-under-the-same-intent.md):
a repeat, never a wait — no `waiting` state and no backoff were added), and LoadCoach reporting
whether it has a remote provider
(lifecycle §3 — a parameter with the safe default `False` until LC-E1 supplies the fact, so the
hybrid egress gate on the planned path is exercised against the fake and cannot fire in a real
deployment before LC-E1). The planner's own spend is recorded on the `plans` row, not the ledger
(lifecycle §4.1).

---

## Phase 8 — Compaction, explanation, operator UI

**Goal:** long trajectories fit their tiers; every trajectory is readable by a human.

**Prerequisites:** Phase 7; `cutctx 0.1.0`.

**Work**
* `TierRouter` compaction trigger ([lifecycle §7](lifecycle.md)); summarization execution on the
  cheapest admissible local tier; `compactions` table; `context.compacted` events; the
  compaction-summary prompt record.
* `ExplanationBuilder`; `promptcadence.trajectory_explanation` 1.0 schema + goldens
  ([ADR-0035](../../adr/0035-application-owned-document-schemas.md));
  `GET /trajectories/{id}/explanation`; `promptcadence trajectory explain`.
* Materialized explanation revisions ([lifecycle §9.1](lifecycle.md)): compose-once at the
  terminal transition into `explanation_revisions` + artifact; live composition for in-flight
  reads; invalidation and re-materialization on retention scrub and re-costing;
  `promptcadence db rebuild-explanations`.
* Operator UI (MirrorWall): trajectory list and timeline detail (plan, turns, tools, debits,
  egress badges, deviations), approvals inbox, tiers, tools, ledger, egress, system pages.
  Server-rendered, progressive enhancement, SSE live updates
  ([ADR-0020](../../adr/0020-ui-rendering-strategy.md)).

**Tests**
* A 100-turn scripted trajectory compacts and completes within its tier budget; the summary call
  is itself a debited, recorded turn; `COMPACTION_FAILED` when the chain cannot fit.
* Explanation goldens; the `materialize(rows) == compose_live(rows)` equality golden, before and
  after a retention scrub and a re-costing (each bumping a revision); a scrubbed-by-retention
  trajectory still explains itself; explanation retrieval within the spec §15 budget regardless
  of turn count; drop-and-rebuild via `rebuild-explanations` reproduces identical documents.
* UI template rendering suite; accessibility checks per UI standards.

**Acceptance criteria**
1. Spec §20 #2's explanation clause holds against the golden; the UI timeline renders every
   record type from a seeded database.

**Known risks:** explanation size for long trajectories — mitigated by JSONL export for the
document and pagination in the UI.
**Gold standards:** every decision reconstructable, forever.
**Deferred:** hardening.

---

## Phase 9 — Hardening, performance, documentation — **1.0 (M12)**

**Goal:** the security checklist held, budgets measured, operations documented, published.

**Prerequisites:** Phase 8. Remote-tier **live** verification additionally requires LC-E1
([roadmap §5](../../roadmap/promptcadence-roadmap.md)).

**Work**
* Security Standards §14 item by item: Host allowlist, CSRF on every form, rate limits, body
  caps, scope enforcement (submit ≠ approve), binding refusals; the prompt-injection test corpus
  (spec §18 security row) run against the full stack; content retention sweep incl. workspaces.
* Performance: every spec §15 budget measured on the reference machine and asserted.
* Package hardening feedback: `cutctx`/`toolyard` 0.2.0 from real-transcript and
  security-pass findings.
* Remote tier end to end behind explicit opt-in, against LC-E1 (recorded transport in CI; one
  marked live run with a real OpenAI-compatible endpoint).
* Operator documentation set (install, configuration reference generated + diff-checked, tier
  and profile guide, security/egress guide, backup/restore, troubleshooting aligned with
  `doctor`); OpenAPI snapshot committed; CHANGELOG; `requirements/ci.lock` cut and audited.
* Gold-standards section verified; publish `promptcadence 1.0.0` (tag is the human step, per suite
  practice).

**Tests**
* The full spec §18 table at full depth; upgrade test from `0.9-beta`; clean-venv install from
  the lock; `pip-audit`/`gitleaks` clean.

**Acceptance criteria**
1. Every spec §20 criterion passes; M12's exit condition demonstrated; verification run on an
   independent brief with explicit permission to say *not ready* (the M7/M8 precedent).

**Known risks:** LC-E1 slipping. The plan tolerates it: 1.0 can ship with remote tiers refusing
honestly (documented), because the refusal *is* specified behaviour — the roadmap records this
release-scope decision explicitly.
**Gold standards:** all of them; this is the phase that proves the list.
**Deferred:** IdeaPress adoptions (M13), trajectory templates, `native.plan`.
