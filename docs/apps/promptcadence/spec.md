# PromptCadence — Specification

**Type:** Application · **Import/distribution name:** `promptcadence` · **Default port:** 8768 · **Env prefix:** `PROMPTCADENCE_`
**Status:** Specified, not implemented. Expanded 2026-09-01 from the design skeleton (`harness.md`);
every open question in that skeleton's §9 is resolved here or in the
[PromptCadence roadmap](../../roadmap/promptcadence-roadmap.md) §2, and each resolution is scheduled as an ADR
before code is written.
**Related:** [Lifecycle](lifecycle.md) · [Development Plan](development-plan.md) ·
[PromptCadence roadmap](../../roadmap/promptcadence-roadmap.md) ·
[CutCtx](../../packages/cutctx/spec.md) · [ToolYard](../../packages/toolyard/spec.md) ·
[LoadLedger](../../packages/loadledger/spec.md) · [Commissioner](../../packages/commissioner/spec.md)

---

## 1. Purpose

Run a multi-turn, tool-using agent loop over LoadCoach in which every step is proposed in a plan,
the plan is approved against governance policy and remaining budget **before any step executes**,
and every turn that does execute is fully reconstructable afterwards — which model ran it, on which
tier, on what data, at what cost, under whose approval. Local models are the default execution
surface; cloud models are an explicitly budgeted, explicitly gated tier, never a silent fallback.

The design principle everything below enforces: **the configurable bypass removes the up-front
planning-and-approval round trip; it never removes per-turn governance.** A bypassed trajectory
still resolves a tier, still checks budget, still checks data classification, still writes the same
audit trail. What it skips is only the extra model call that drafts a whole plan before step one,
and the policy pass over that plan. This distinction is load-bearing and is recorded as its own ADR
(roadmap §2, D-4) so it cannot erode during implementation.

## 2. Scope

* Trajectories: a persisted, resumable unit of agent work with a state machine, a lease and a
  recovery path.
* Planning: drafting a structured `Plan` via LoadCoach, validating it against PromptCadence's own schema,
  approving it step by step against tier policy, budget and egress governance.
* Tiers: named execution surfaces (`local_fast`, `remote_frontier`, …), each mapping to one
  LoadCoach task profile, an egress class and a data-classification ceiling. Configuration over
  LoadCoach, never routing math of PromptCadence's own.
* The agent loop: turn execution through LoadCoach `/generate`, tool execution through ToolYard,
  transcript ownership, context compaction through CutCtx, advance on declared
  `finish_reason` only.
* Governance: per-turn egress evaluation (Commissioner), budget accumulation and ceilings (LoadLedger),
  immutable per-step `ExecutionIntent`s minted by approval, category-typed deviation detection
  against the turn's intent, scoped re-approval as intent supersession, human-in-the-loop approval
  as a configurable mode.
* Explainability: a complete reconstructable record per trajectory, composing LoadCoach's own
  routing explanations with plan, approvals, tool calls, ledger entries and egress decisions.
* Web UI (operator console), CLI, public HTTP API, SSE streams with replay.

## 3. Explicit non-goals

* **No routing math of its own.** Tier membership is PromptCadence's constraint; *which model within a
  tier* is LoadCoach's filter → score → rank → select, unchanged
  ([Routing](../loadcoach/routing.md)).
* **No provider access.** PromptCadence never imports ModelRack and never talks to Ollama or a cloud
  endpoint directly; every generation goes through LoadCoach's HTTP API. A harness that could
  reach a provider directly would have a second, ungoverned egress path, which is exactly what
  this application exists to prevent.
* **No benchmark execution or scoring.** Capability evidence for tier suitability reaches routing
  through LoadCoach's normal FreeWeight evidence import; PromptCadence neither produces nor imports
  evidence.
* **No content-workflow decisions.** PromptCadence runs tasks handed to it; deciding what a task should
  accomplish is IdeaPress's kind of problem.
* No imports of FreeWeight, LoadCoach or IdeaPress as code — all suite interaction is HTTP with
  versioned payloads, per [master architecture §6](../../architecture/master-architecture.md).
* No advancing a trajectory on the model's prose. The loop advances only on a declared
  `finish_reason` and schema-validated structure — Python decides control flow, never the model
  (the same principle as [ADR-0033](../../adr/0033-benchmark-interaction-protocol.md) and
  [ADR-0039](../../adr/0039-audit-gated-blocking-requirements.md)).
* No standalone chat product. The web UI is an operator console for trajectories, approvals,
  budgets and egress — not a conversational interface.
* No multi-machine execution; no broker, no Redis, no Celery
  ([ADR-0010](../../adr/0010-queue-implementation.md)).
* No direct-provider fallback mode. Unlike IdeaPress, PromptCadence without LoadCoach has no meaningful
  degraded execution — tiers are defined over LoadCoach's routing — so an unreachable LoadCoach
  parks work with a reason rather than executing around it (§13). LoadCoach is still not required
  at startup.

## 4. Responsibilities

| Area | Responsibility |
|---|---|
| Trajectories | Persistence, state machine, lease-based background execution, recovery, cancellation |
| Planning | Draft via LoadCoach (`tools.plan` profile), validate against PromptCadence's plan schema, bounded structured-output retry |
| Approval | Automated policy review per step; hybrid/manual human approval; per-step verdicts with reasons; approval's output is the minting of immutable `ExecutionIntent`s ([Lifecycle §4.3](lifecycle.md)) |
| Tiers | Tier configuration and validation; tier → task-profile resolution; per-tier context budgets |
| Loop | DAG-aware step scheduling; per-turn LoadCoach calls; tool round trips; finish-reason handling |
| Tools | Registry composition, per-trajectory allowlists, sandboxed execution via ToolYard |
| Context | Compaction requests to CutCtx; execution of planned summarizations via LoadCoach |
| Budget | Ceiling configuration (per-trajectory, per-day, per-project), pre-flight estimates, per-turn debits via LoadLedger; on exhaustion: halt, a ceiling-raise approval, or a wait for the next UTC day, per ceiling |
| Egress | Per-turn classification-vs-tier evaluation via Commissioner; durable decisions, approvals and denials alike |
| Deviation | One pure comparison per turn against its `ExecutionIntent`, category-typed ([Lifecycle §5](lifecycle.md)); recorded-continue / scoped re-approval (a superseding intent revision) / halt per severity and policy |
| Explainability | Full trajectory record, composable and exportable, retained forever by default; served from materialized revisions for terminal trajectories ([Lifecycle §9.1](lifecycle.md)) |
| Interfaces | Web UI, CLI, public API `/api/v1`, SSE streams with replay |

## 5. Dependencies

**Suite:** `baseaicore` (≥ 0.4.1 — `DataClassification`), `setspec` (≥ 0.5 —
`governance.egress_decision`), `weightsdb`, `mirrorwall`, `cutctx`, `toolyard`, `loadledger`,
`commissioner`.
**Deliberately absent:** `modelrack` (no provider access — §3) and `sweatmeter` (telemetry is
displayed from LoadCoach's `/system/status`, the same way IdeaPress treats it).
**Third party:** `fastapi`, `uvicorn[standard]`, `typer`, `pydantic`, `pydantic-settings`,
`sqlalchemy`, `alembic`, `jinja2`, `httpx` (the LoadCoach client).
**External services:** a running LoadCoach (for execution, not for startup).

**Required at startup:** none. PromptCadence starts and serves with no LoadCoach reachable, reporting
degraded health; trajectories submitted in that state queue with a reason.

## 6. Consumers

* **Users** — web UI and CLI, submitting trajectories and acting on approval requests.
* **External tools** — the public API, the same way they consume LoadCoach's.
* **IdeaPress** — none required for v1. Named as a future consumer of a PromptCadence trajectory for its
  unimplemented `research` stage ([Workflows §2](../ideapress/workflows.md)); no coupling in either
  direction until that ADR is written.

## 7. Public APIs

### 7.1 HTTP (`/api/v1`)

```text
GET  /health                      GET  /version                    GET  /system/status
POST /trajectories                GET  /trajectories               GET  /trajectories/{id}
GET  /trajectories/{id}/stream    POST /trajectories/{id}/cancel   GET  /trajectories/{id}/explanation
POST /trajectories/{id}/approve   POST /trajectories/{id}/deny     GET  /trajectories/{id}/turns
GET  /approvals                   GET  /tiers                      GET  /tools
GET  /ledger                      GET  /ledger/entries             GET  /egress-decisions
GET  /settings                    PUT  /settings
```

* `POST /trajectories` — `task`, `data_classification` (default `"confidential"` — the safe
  default; unclassified data is treated as most restrictive), optional `budget`
  (`money` and/or `tokens`, and `partial_pricing` — `"floor"` or `"strict"`, a per-request
  override of `[budget] partial_pricing`; absent means "the configured default", which is not the
  same as either value pinned), optional `project` (must name a configured
  `[budget.projects.<name>]`, else `PROJECT_UNKNOWN`; every debit is tagged `project:<name>` and
  the project's ceiling binds), optional `tools` allowlist (must be a subset of the registry),
  optional `bypass_planning`, optional `tier` pin (recorded as an override; policy still applies),
  optional `max_steps`/`max_turns` within configured caps.
* `GET /trajectories/{id}/explanation` — the full reconstructable record (§11 contract 2),
  mirroring LoadCoach's `/jobs/{id}/explanation` in spirit and linking to each underlying
  LoadCoach explanation by job id.
* `POST /trajectories/{id}/approve|deny` — resolves a pending approval request (plan-level or
  scoped per-step re-approval). Requires the `approve` scope. Idempotent per approval request.
* `GET /trajectories/{id}/stream` — SSE per
  [API Standards §8](../../standards/api-and-contract-standards.md): every event a persisted row,
  replay from `Last-Event-ID` ([ADR-0044](../../adr/0044-a-state-change-and-its-event-are-one-write.md)).

### 7.2 CLI

```text
promptcadence serve | health | doctor | version
promptcadence config show|validate|init|path|reference
promptcadence db upgrade|status|backup|restore
promptcadence run "<task>" [--classification …] [--budget …] [--tokens …] [--tier …]
            [--bypass-planning] [--tool …] [--follow] [--json]
promptcadence trajectory list|show|cancel|wait|explain
promptcadence approvals list           promptcadence approve <id> | deny <id> [--reason …]
promptcadence tiers list|show|check    # check: verifies each tier's task profile exists in LoadCoach
promptcadence tools list|show
promptcadence ledger show [--scope day|project|tier|trajectory] [--trajectory <id>] [--json]
promptcadence egress list [--denied-only] [--verdict approved|denied|violation]
                                       [--trajectory <id>] [--limit N] [--json]
promptcadence token create|list|revoke
```

## 8. Inputs

Trajectory requests (task, classification, budget, project label, tool allowlist, overrides),
tier and policy configuration, tool registry configuration, approval verdicts from operators,
LoadCoach responses (results, routing metadata, usage, tool-call requests), configuration.

## 9. Outputs

Trajectory records and state transitions, plans, per-step approvals and the `ExecutionIntent`s
they mint (every revision retained), turns with full provenance (model identity, tier, the
`(intent_id, revision)` executed under, `TokenUsage`, timings — LoadCoach time and PromptCadence
overhead separately), tool-call records, ledger entries, egress decisions, compaction events, the
composed explanation document with its materialized revisions, SSE event streams, typed errors.

**The explanation is an application-owned document** — schema name
`promptcadence.trajectory_explanation`, version `1.0`, published under the ADR-0035 namespace
([ADR-0035](../../adr/0035-application-owned-document-schemas.md)): no other application reads a
PromptCadence plan directly in v1, so it does not enter SetSpec (roadmap §2, D-7). The egress decisions
*inside* it are SetSpec `governance.egress_decision` payloads, because IdeaPress's egress badge is
a named second consumer of that shape.

**Caller prompts are passed through unmodified**, and PromptCadence's own prompt records — the planner
prompt, the compaction-summary prompt, the structured-output corrective — are versioned JSON
records ([ADR-0012](../../adr/0012-prompt-storage-format.md), `setspec.prompts`), each recorded on
the turn that used it with its `prompt_id`, `version` and `sha256`.

## 10. Data ownership

Owns `promptcadence.sqlite3` exclusively: `trajectories`, `tier_snapshots`, `plans`, `plan_steps`,
`plan_approvals`, `approval_requests`, `execution_intents` (immutable, revisioned —
[Lifecycle §4.3](lifecycle.md)), `deviations`,
`threads`, `turns`, `tool_call_records`, `ledger_entries` (mounted from `loadledger.sql`),
`egress_decisions` (mounted from `commissioner.sql`), `compactions`, `explanation_revisions`
(derived cache — [Lifecycle §9.1](lifecycle.md)), `events`, `api_tokens`, `settings`. One Alembic history, owned by PromptCadence, including the mounted package
tables ([roadmap §2, D-6](../../roadmap/promptcadence-roadmap.md)). No access to any other application's
database, ever.

`tier_snapshots` is **content-addressed**: its primary key is the digest of the tier definitions
it holds, so identical configurations share one row and an edited ceiling produces a new one with
no migration. `trajectories.tier_snapshot_id` carries no foreign key to it deliberately — a
snapshot is shared by every trajectory whose configuration matched, so a cascade from one
trajectory must never reach it. `deviations` is the row half of "every deviation is an event and
a row" ([Lifecycle §5](lifecycle.md)); the earlier omission of both tables from this list was a
defect, closed in Phase 2 with migration `0002`.

**Thread and turn state is PromptCadence-internal.** The skeleton proposed a `ThreadRack` package and
flagged that it alone had no second consumer; per the suite's own extraction rule —
"nothing is extracted with fewer than two real consumers"
([ADR-0011](../../adr/0011-shared-package-boundaries.md)) — it is **not** created. PromptCadence builds
`promptcadence/domain/threads.py` and its store as if they were a package (no PromptCadence vocabulary in the
types), so extraction is a move, not a rewrite, if a second consumer appears. Recorded as a
deliberate rejection, like `LoadCoachClient`.

## 11. Public contracts

1. **Governance invariance — structural, via the `ExecutionIntent`.** There is no code path that
   executes a turn without an immutable `ExecutionIntent` to check it against
   ([Lifecycle §4.3](lifecycle.md)): the planned path mints one per approved step, the bypass path
   mints one default intent from `TierPolicy`. Planned and bypassed trajectories therefore produce
   records identical in shape except for the `plan`/`plan_approvals` rows — tier resolution,
   classification checks, budget debits, egress decisions and the deviation comparison occur under
   an intent on every turn in both modes. A test diffs the two record sets to prove it.
2. **Explanation contract.** Every trajectory yields a complete record — plan (when planned),
   approvals, every intent revision, every turn with its intent reference and LoadCoach
   explanation reference, every tool call, every ledger entry, every egress decision, in turn
   order — retrievable for the lifetime of the trajectory. Terminal trajectories are served from a
   materialized revision that is a **derived cache, never the source of truth**: it is rebuilt
   whenever the authoritative rows change, and `materialize(rows) == compose_live(rows)` is a
   golden-tested equality ([Lifecycle §9.1](lifecycle.md)).
3. **Egress contract.** No request whose data classification exceeds the target tier's
   `max_data_classification` is ever sent; the refusal is recorded as an `EgressDecision` exactly
   as an approval would be. A declined call is as auditable as an approved one.
4. **Verification contract.** Every LoadCoach response's execution subject (model identity,
   provider kind) is checked against the tier that requested it. A local-tier turn answered by a
   remote provider halts the trajectory and records the violation — the tier constraint is
   verified, not assumed ([ADR-0043](../../adr/0043-grounding-is-verified-not-assumed.md)'s
   principle applied to egress). **Provider kind alone cannot settle it**: `ProviderKind` names a
   runtime, and `openai_compatible` covers both a local llama.cpp server and a paid remote
   endpoint, so deriving egress from the kind would be exactly the assumption this contract
   forbids. The egress class is resolved at the HTTP boundary — while LoadCoach serves one
   configured provider, verifying that the response's provider *is* the configured one is the
   verification — and LC-E1's multi-provider registration must carry the serving provider's
   identity on every response so the resolution stays a check rather than an inference.
   **Absence is a violation, not a pass**: a response naming no execution subject, and a surface
   with no single configured provider to check against, both mean that something answered and
   nothing here can establish that it was the tier that promised to. Both halt and record a
   `VIOLATION` `EgressDecision` under the verification step's own policy name, never under the
   evaluating policy's — that policy answers "may this go?" before the fact and never produced
   this verdict ([ADR-0054](../../adr/0054-commissioner-records-the-caller-enforces.md) rule 7).
5. **Budget contract.** Money ceilings govern priced usage; token ceilings govern all usage. A
   local model's cost is `UNSUPPORTED`, never `$0.00`
   ([ADR-0030](../../adr/0030-model-cost-and-pricing.md)); a remote tier with no configured
   `ModelPricing` record is refused with `UNPRICED_EGRESS_REFUSED` — unpriced egress is refused,
   not free, because a ceiling cannot bind what cannot be priced. The check is on the **record**,
   not on the `pricing_file` field: startup validation already refuses a remote tier that names no
   file at all (§12), so what reaches the pre-flight is a tier whose price list holds no record
   claiming the current instant — an expired list, or one that never covered the tier. A priced response the provider
   did not fully report accumulates as a floor and is rendered as one ("at least", never a bare
   figure); `[budget] partial_pricing = "strict"` makes such a response exceed the money ceiling
   instead, for budgets that must not be crossed
   ([ADR-0069](../../adr/0069-a-partial-price-is-a-floor-and-a-money-ceiling-chooses-how-it-binds.md)).
   Three ceilings are active on a labelled trajectory: its own (the request's `budget` or the
   configured default), the per-day ceiling every trajectory shares, and its project's; the most
   restrictive binds. The per-day ceiling is what lets any amount of work run while only so much
   is spent: local work is unpriced and never counts against it, and under the `window` policy a
   trajectory it stops parks until the next UTC day instead of halting.
6. **Advance contract.** A step completes only on a declared `finish_reason` of `STOP` (or a
   schema-validated structured result); `LENGTH`, `ERROR` and absence are handled explicitly, never
   read as success. **The declared finish must be on the wire.** LoadCoach renders the provider's
   declared reason at `output.finish_reason` in both the `/generate` response and the job
   document since its commit `846348b` (before it, the reason was recorded per
   attempt and rendered nowhere — the gap `D2_HANDOFF.md` §2 named); the job document also
   carries the validation `checks`, so a turn reconciled after a crash is judged on the same
   facts as one read from the response. PromptCadence reads that field and nothing else for the
   declared finish, and treats its absence — an older LoadCoach — as *absence*: a halt naming the
   gap, never a completion. A declared `LENGTH` or `ERROR` wins over a passed schema check.
7. **Streaming contract.** SSE with persisted events and `Last-Event-ID` replay, the same envelope
   shape as the rest of the suite.
8. **Degradation contract.** No LoadCoach, no remote provider configured, or an exhausted budget
   each produce a documented behaviour with a reason — never a silent fallback, never a failure to
   serve the API.

## 12. Configuration

`~/.config/promptcadence/config.toml`, `PROMPTCADENCE_*` environment variables, CLI flags, per
[Configuration Standards](../../standards/configuration-standards.md). Principal sections:

```toml
[server]        host = "127.0.0.1"  port = 8768  allow_lan_exposure = false
                allowed_hosts = []  rate_limit_per_minute = 600  max_body_bytes = 1048576
[storage]       database_url = "sqlite:///<data>/promptcadence.sqlite3"  auto_migrate = true
                content_retention_hours = 24    # transcript text; records/hashes kept forever
                retain_content = false          # config-only, mirrors LoadCoach
[loadcoach]     base_url = "http://127.0.0.1:8766"
                api_key_env = ""                # or api_key_file (ADR-0026)
                timeout_seconds = 600
[planning]      enabled = true                  # the bypass switch — governance is never bypassed
                allow_request_override = true   # permit per-request bypass_planning
                reapproval_scope = "on_tier_or_classification_change"   # or "any_deviation"
                max_plan_steps = 20
                corrective_retries = 2          # retries after an invalid draft, every issue fed
                                                # back at once (lifecycle §4.1); then
                                                # PLAN_DRAFT_FAILED. 0 = one attempt, no retry
[approval]      mode = "auto"                   # auto | hybrid | manual
                # hybrid: auto-approve except steps matching the gates below
                gate_egress_at = "internal"     # egress at/above this classification needs a human
                gate_step_cost = { currency = "USD", nanos = 1_000_000_000 }   # $1.00/step
                request_timeout_hours = 24      # pending approvals expire → trajectory halts
[execution]     max_concurrent_trajectories = 1
                max_concurrent_steps = 1        # >1 dispatches only across disjoint surfaces
                max_concurrent_remote_steps = 2
                max_turns_per_step = 8          # tool round trips within one step
                max_steps = 20                  # bypass mode: max turns in the direct loop
                lease_seconds = 60              # trajectory worker lease (recovery per ADR-0036)
[budget]        default_money_ceiling = { currency = "USD", nanos = 5_000_000_000 }   # $5.00
                default_token_ceiling = 2_000_000
                daily_money_ceiling  = { currency = "USD", nanos = 20_000_000_000 }   # $20.00
                estimate_min_samples = 20       # historical estimator threshold (lifecycle §6)
                partial_pricing = "floor"       # floor | strict (ADR-0069). How a money ceiling
                                                # treats a priced response the provider did not
                                                # fully report. floor: bind on what was priced,
                                                # may fire late. strict: treat it as exceeded —
                                                # never crosses the cap; with today's adapters
                                                # this halts on the first remote response
                on_exhausted = "approval"       # approval | halt — per-trajectory and per-project
                                                # ceilings never reset, so waiting is not offered
                on_daily_exhausted = "window"   # window | approval | halt — window parks the
                                                # trajectory until the next UTC day (lifecycle §8)
                window_wait_max_days = 3        # then halted with the cause; never waits forever
                # There is deliberately **no** daily_token_ceiling. The per-day ceiling caps what
                # is *spent* in a day, and local work is unpriced and never counts against it
                # (§11.5); a per-day token cap would stop the local half of an installation at
                # midnight for something nobody budgeted. The universal brake is the
                # per-trajectory token ceiling, which binds every turn on every tier.
[budget.projects.research]
                money_ceiling = { currency = "USD", nanos = 50_000_000_000 }   # $50.00, lifetime:
                                                # every trajectory labelled project = "research",
                                                # every day, until raised
                # token_ceiling = 100_000_000   # optional; a project binding neither is refused
[tools]         enabled = ["read_file", "list_dir", "write_file", "run_command", "http_fetch"]
                                                # All five are registered from P6. Before it,
                                                # http_fetch was listed here and withheld from the
                                                # registry with a named cause, because a fetch tool
                                                # without egress governance is the hole this
                                                # application exists to close. The governance now
                                                # exists, so the tool is real and nothing shipped
                                                # is withheld.
                workspace_root = ""             # default: <data>/workspaces; per-trajectory subdir
                artifact_root = ""              # default: <data>/artifacts; oversize tool output,
                                                # keyed by the digest of the whole output
                read_roots = []                 # extra read-only roots (allowlist). Absolute, and
                                                # disjoint from workspace_root — an overlap is a
                                                # startup refusal, because the path half and the
                                                # subprocess half would disagree about it
                fetch_allowed_hosts = []        # http_fetch hosts beyond loopback (ADR-0026 §3).
                                                # This answers "may anyone reach this host"; the
                                                # ceiling below answers "may THIS trajectory's data
                                                # reach it". Two gates, two questions.
                # fetch_max_data_classification = "internal"
                                                # The ceiling http_fetch's non-loopback egress is
                                                # governed by. **Absent by default**, which denies
                                                # every non-loopback fetch with
                                                # `no_ceiling_declared` — an undeclared ceiling is
                                                # never assumed public (ADR-0046 rule 3). Loopback
                                                # needs none: it is not egress, and is approved
                                                # with `target_not_remote`.
                redact_args = []                # tool names whose args are stored as hash only
                container_image = "python:3.12-slim"    # run_command's container rung. Probed and
                                                # run with --pull=never, so it must already be
                                                # present; `doctor` shows the rung and why
                max_result_chars = 8192         # what the model sees of a result; the whole output
                                                # is kept as an artifact under its digest, never a
                                                # truncated body pretending to be the whole one
                timeout_seconds = 30.0          # per call; no way to express "no timeout"
[compaction]    threshold = 0.8                 # compact when estimate > 0.8 × tier context budget
                policy_chain = ["observation_masking", "summarizing", "drop_oldest"]
                protected_recent_turns = 4
[tiers.local_fast]
                task_profile = "tools.agent.local_fast"
                remote = false                  # local ⇒ max classification implicitly confidential
                context_budget_tokens = 16384
[tiers.local_large]
                task_profile = "tools.agent.local_large"
                remote = false
                context_budget_tokens = 32768
[tiers.remote_cheap]
                task_profile = "tools.agent.remote_cheap"
                remote = true
                max_data_classification = "internal"      # never confidential
                context_budget_tokens = 128000
                pricing_file = ""               # required for a remote tier. A ModelPricing
                                                # record file per ADR-0072 — JSON, a `records`
                                                # array, rates as decimal strings, an omitted rate
                                                # meaning "not stated" rather than free. Read once
                                                # at startup; an unreadable one refuses the boot.
                default_step_input_tokens = 4096   # the estimator's configured_default rung
                default_step_output_tokens = 1024  # (lifecycle §6). Two numbers, not one total:
                                                # the classes price differently, and a total split
                                                # by a fixed ratio would be a magic number between
                                                # an operator and the cap that binds them.
[tiers.remote_frontier]
                task_profile = "tools.agent.remote_frontier"
                remote = true
                max_data_classification = "public"
                context_budget_tokens = 200000
                pricing_file = ""
[policy]        default_tier = "local_fast"     # bypass mode and unplanned turns start here
                escalation_order = ["local_fast", "local_large", "remote_cheap", "remote_frontier"]
[logging]       level = "INFO"  include_content = false
```

Startup validation refuses: a remote tier without `max_data_classification`; a remote tier without
a pricing source; an unknown classification value; a tier naming no task profile; non-loopback
binding without authentication; `approval.mode = "manual"` with no `approve`-scoped token defined;
a relative `tools.workspace_root`, `tools.artifact_root` or read root; and a read root that equals,
contains or sits inside the workspace root — that last one because containment's path half and its
subprocess half would then disagree about the same directory, and the disagreement would surface
on one tool call of one trajectory rather than at startup.
Tier names are operator-chosen; the four above are the documented defaults, not a fixed taxonomy
(roadmap §2, D-3). **Two of them ship active and two ship commented out**, and the reason is this
section's own validation: a remote tier with an empty `pricing_file` is refused at startup, so
shipping `remote_cheap` and `remote_frontier` as active defaults would make a zero-configuration
`promptcadence serve` refuse to boot — breaking §20 AC1. So `config.py` ships `local_fast` and
`local_large` active, and the shipped `config.toml` example carries the remote pair commented out
with the pricing line an operator has to fill in. Uncommenting one without supplying a pricing
source is refused, loudly, at startup; a correctly configured one that LoadCoach cannot serve
halts with `TIER_UNAVAILABLE` and the reason `loadcoach_has_no_remote_provider` — which
`TierPolicy` decides before any call is made, so a remote tier is never *quietly* served by a
local model.

The tier task profiles (`tools.plan`, `tools.agent.local_fast`, `tools.agent.local_large`,
`tools.agent.remote_cheap`, `tools.agent.remote_frontier`) are **LoadCoach configuration, not
code** — namespaced specializations of the shipped `tools.agent` profile, and they **ship in
LoadCoach's `src/loadcoach/config/task_profiles.toml`**
([ADR-0047 §1](../../adr/0047-a-tier-is-configuration-and-a-model-never-sizes-its-own-budget.md),
[LoadCoach routing §2](../loadcoach/routing.md)), not in a document here that an operator would
have to transcribe. PromptCadence vendors that file as a contract snapshot and pins its digest, so
a tier naming a profile LoadCoach no longer ships fails CI rather than an operator's first turn.
`promptcadence doctor` and `promptcadence tiers check` verify each configured tier's
profile exists in the running LoadCoach, the way I7 verifies IdeaPress's task map.

## 13. Error behaviour

```text
TRAJECTORY_NOT_FOUND        PLAN_DRAFT_FAILED         BUDGET_EXCEEDED
TRAJECTORY_NOT_CANCELLABLE  PLAN_INVALID              TOKEN_BUDGET_EXCEEDED
CLASSIFICATION_INVALID      PLAN_REJECTED             UNPRICED_EGRESS_REFUSED
TIER_NOT_CONFIGURED         APPROVAL_REQUIRED         EGRESS_DENIED
TIER_UNAVAILABLE            APPROVAL_INVALID_STATE    TOOL_NOT_FOUND
LOADCOACH_UNAVAILABLE       DEVIATION_HALTED          TOOL_ARGS_INVALID
LOADCOACH_ERROR             STEP_LIMIT_EXCEEDED       TOOL_REFUSED
SCHEMA_VERSION_UNSUPPORTED  COMPACTION_FAILED         TOOL_EXECUTION_FAILED
PROJECT_UNKNOWN
```

Every error LoadCoach can return has exactly one mapping here, so no LoadCoach failure reaches a
caller as `INTERNAL_ERROR`:

| LoadCoach code | PromptCadence behaviour | Surfaced as |
|---|---|---|
| `PROVIDER_UNAVAILABLE`, `PROVIDER_TIMEOUT` | Retry per step policy, then fallback per plan, then halt | `LOADCOACH_ERROR` with the original code in `details` |
| `NO_ELIGIBLE_MODEL` | The tier cannot serve this step now; fall to the intent's next `fallback_tier`, else raise a `tier_escalation` deviation (scoped re-approval) or halt | `TIER_UNAVAILABLE`, candidates and reasons preserved |
| `QUEUE_FULL`, `MAX_WAIT_EXCEEDED` | Trajectory waits with a reason, then halts | `LOADCOACH_ERROR` |
| `CONTEXT_LIMIT_EXCEEDED` | Trigger compaction and retry once; then halt | `COMPACTION_FAILED` if compaction cannot fit it |
| `VALIDATION_FAILED`, `STRUCTURED_OUTPUT_INVALID` | LoadCoach's own retry/fallback ran; PromptCadence records and applies its bounded corrective (planner only) | `PLAN_DRAFT_FAILED` / `LOADCOACH_ERROR` |
| connection refused / DNS | Trajectory parks in `waiting` with the reason; health degraded | `LOADCOACH_UNAVAILABLE` |
| `TASK_PROFILE_NOT_FOUND` | The tier is configured here and cannot be served there; `promptcadence tiers check` names it | `TIER_UNAVAILABLE` with `reason = task_profile_not_found` |
| Every other LoadCoach code — `PROVIDER_PROTOCOL_ERROR`, `PROVIDER_REJECTED`, `ALL_CANDIDATES_FAILED`, `MODEL_NOT_FOUND`, `CAPABILITY_UNSUPPORTED`, `INSUFFICIENT_RESOURCES`, `GENERATION_CANCELLED`, `JOB_NOT_FOUND`, `JOB_NOT_CANCELLABLE`, its web layer's `VALIDATION_ERROR`, `UNAUTHORIZED`, `FORBIDDEN`, `RATE_LIMITED`, `INTERNAL_ERROR`, and any code this build does not know | Halt with the cause naming the code | `LOADCOACH_ERROR` with the original code and details preserved — never `INTERNAL_ERROR` |
| The client's own read timeout | Cancel the job the request may have started, then halt | `LOADCOACH_ERROR` with `reason = client_timeout` |

The mapping is complete from Phase 3 (`infrastructure/loadcoach.py`, `LOADCOACH_CODE_MAP`, walked
by a test against LoadCoach's own spec §13 list); the *behaviour* column is the target. Until the
step policy arrives with the plan (Phase 7), the "retry" and "wait" cells halt with the cause. And
there is no `waiting` state in [Lifecycle §8.1](lifecycle.md): until one is specified, an
unreachable LoadCoach mid-turn is T13 (`failed`) with the cause, after cancelling any job the
request may have started — never a silent retry of the same request.

Behavioural rules:

* `PLAN_REJECTED` always lists every step's verdict and the policy or ceiling that rejected it —
  "the plan was refused" without numbers is a defect.
* A denied egress evaluation is not an exception path: the turn ends with a structured refusal
  recorded in the transcript, the `EgressDecision` is persisted, and the deviation policy decides
  whether the trajectory continues on a permitted tier or halts.
* A tool refusal (sandbox, allowlist, schema) is returned to the model as a structured
  `ToolResult`, never an exception, never a crash — one refused tool call does not end a
  trajectory ([ToolYard §11](../../packages/toolyard/spec.md)).
* Budget exhaustion mid-trajectory transitions to `awaiting_approval` (a ceiling raise is an
  approval) or halts, per `on_exhausted`; exhaustion of the per-day ceiling may instead park the
  trajectory in `awaiting_window` until the next UTC day (`on_daily_exhausted = "window"`), for at
  most `window_wait_max_days`, then halts. It never silently continues, and never waits forever.
* Cancellation is honoured at the next turn boundary and cancels any in-flight LoadCoach job.
* Every halt names its cause; `promptcadence trajectory show` prints it verbatim.

## 14. Security considerations

PromptCadence concentrates the two riskiest behaviours in the suite — executing model-directed tool
calls, and sending data to paid remote providers — so its security posture is the strictest.

* **Trust boundary: model output is untrusted input.** Tool names, tool arguments and plan content
  originate from a model and may be adversarial (prompt injection through tool results is assumed).
  Consequences, each tested: only registry-listed tools are callable regardless of what the model
  asks for; arguments are schema-validated then sandbox-checked; egress policy is evaluated from
  the *trajectory's* declared classification, never from model text; no model output is ever
  interpolated into a shell command, a path outside containment, or a fetch URL that skips
  [ADR-0026 §3](../../adr/0026-local-http-hardening.md) checks.
* **Sandbox:** all filesystem tools operate under per-trajectory workspace containment (symlinks
  resolved before checks); `run_command` executes under ToolYard's tiered isolation, reusing the
  FreeWeight precedent — container → bwrap → **refuse**
  ([ADR-0018](../../adr/0018-external-benchmark-isolation.md)); `http_fetch` obeys the scheme,
  host-allowlist, literal-IP, redirect and size rules of ADR-0026 §3.
* Loopback default; non-loopback requires tokens plus the exposure acknowledgement; `Host` header
  allowlist before routing and before authentication.
* Scopes: `read` (status, trajectories, explanations), `write` (submit, cancel), `approve`
  (resolve approval requests — deliberately separate from `write`, so the identity that submits
  work cannot approve its own egress), `admin` (settings, tokens).
* Transcript and tool-output text follows LoadCoach's retention model: kept
  `content_retention_hours` after a trajectory finishes, then swept; hashes, usage, decisions and
  events stay, so the trajectory remains explicable. A scrubbed turn says "content removed by
  retention".
* The LoadCoach API key is read from an environment variable or file, never config plaintext,
  never logged, never in `details`.
* Every remote-tier selection is marked as egress in the UI, the API response and the explanation
  — the same badge convention LoadCoach already uses, now backed by a durable `EgressDecision`.

## 15. Performance considerations

| Measure | Target | Ceiling |
|---|---|---|
| Trajectory admission (accepted → persisted) | ≤ 50 ms | 200 ms |
| Plan approval evaluation, 20 steps | ≤ 20 ms | 100 ms |
| Per-turn overhead excluding LoadCoach time | ≤ 25 ms | 100 ms |
| Tool dispatch overhead excluding tool runtime | ≤ 10 ms | 50 ms |
| Ledger debit, ceilings evaluated | ≤ 5 ms | 20 ms |
| Compaction plan, 200-turn transcript | ≤ 50 ms | 200 ms |
| Added latency per SSE event | ≤ 5 ms | 20 ms |
| Explanation retrieval, terminal trajectory (materialized), any size | ≤ 25 ms | 100 ms |
| Explanation materialization at terminal transition, 500-turn trajectory | ≤ 2 s | 10 s |
| Recovery of 100 in-flight trajectories at startup | ≤ 2 s | 10 s |

LoadCoach time, tool time and PromptCadence overhead are always reported separately, per turn and per
trajectory. Egress evaluation and the deviation comparison carry no budgets of their own: both are
pure in-memory comparisons sitting beside model calls measured in seconds, and they are already
inside the per-turn overhead budget — a separate microsecond-scale target would be measurement
fuss, not protection.

## 16. Cross-platform considerations

Linux tier 1, matching the suite. The API, queue, planning, ledger and egress machinery are pure
Python and portable. `run_command`'s isolation tiers degrade per ToolYard's platform table —
where no sandbox tier is available the tool **refuses** with a recorded reason rather than
executing unisolated; all other tools work everywhere.

## 17. Observability

* Structured logs with `request_id`, `trajectory_id`, `step_id`, `turn_id`, `tier`,
  `model_canonical_id`.
* Persisted events with SSE replay; a state change and its event are one write
  ([ADR-0044](../../adr/0044-a-state-change-and-its-event-are-one-write.md)). Event types:
  `trajectory.created`, `trajectory.claimed`, `plan.drafted`, `plan.approved`, `plan.rejected`,
  `approval.requested`, `approval.granted`, `approval.denied`, `intent.minted`, `step.started`,
  `turn.started`, `turn.completed`, `tool.call.started`, `tool.call.completed`,
  `context.compacted`, `budget.debited`, `budget.window_wait`, `egress.evaluated`,
  `deviation.detected` (carrying the category), `step.completed`, `trajectory.completed`,
  `trajectory.resumed`, `trajectory.halted`, `trajectory.failed`, `trajectory.cancelled`,
  `trajectory.recovered`. The emitting transition for each is the
  [Lifecycle §8.2](lifecycle.md) table.
* Health components: `database`, `loadcoach` (reachability + version), `tiers` (each configured
  tier's task profile resolvable), `sandbox` (which isolation tier is available), `ledger`
  (daily ceiling headroom). A missing remote provider degrades the remote tiers' component with a
  reason; it is never a failure to serve.
* `GET /api/v1/system/status`: active trajectories, pending approvals with ages, today's ledger
  position against the daily ceiling and against each configured project's ceiling, per-tier
  availability, LoadCoach queue passthrough.
* Every governance decision persisted in full — 100 %, not sampled, matching LoadCoach's routing
  explanations.

## 18. Test strategy

| Layer | Coverage |
|---|---|
| Unit | Tier resolution; classification lattice; plan validation; approval policy (auto/hybrid/manual); intent minting (plan, redline, bypass default, superseding revision — immutability asserted); the deviation comparison over the full category × severity × scope matrix; estimator source selection; every Lifecycle §8.2 transition and every §8.1 illegal one refused; compaction triggering |
| Contract | Plan schema goldens; explanation document goldens (`promptcadence.trajectory_explanation` 1.0); `materialize(rows) == compose_live(rows)` equality goldens, including post-retention-scrub and post-re-costing revisions; API against the OpenAPI snapshot; error codes; SSE shape; `governance.egress_decision` payloads against SetSpec goldens |
| Integration | Full loop against a **fake LoadCoach HTTP server** (recorded response shapes, scriptable failures); queue lease/recovery after simulated crash; mounted `loadledger.sql`/`commissioner.sql` tables on both dialects |
| E2E | Submit → plan → approve → execute (tools + compaction + debits + egress) → explanation, over HTTP and CLI; the same journey with `bypass_planning` diffed for contract 1; manual-approval journey including deny |
| Failure-path | LoadCoach down mid-turn; plan invalid after retries; egress denied mid-trajectory; budget exhausted mid-step under each of the halt, approval and window policies; an unknown project refused; tool sandbox refusal; deviation → re-approval → deny; approval timeout; kill −9 recovery |
| Security | Unlisted tool requested by the model; path escape and symlink escape attempts; fetch to a non-allowlisted host; confidential data with a remote tier pin; scope enforcement (submit ≠ approve); no secret in logs |
| Performance | Every budget in §15 |
| Live (marked) | Real LoadCoach + Ollama: a local-tier planned trajectory with one tool call, end to end |

Coverage floor: **85 %** (application). The default suite passes with no LoadCoach, no Ollama, no
GPU and no network.

## 19. Compatibility and versioning

* Application semver; API `v1`; the explanation document versioned as
  `promptcadence.trajectory_explanation` `MAJOR.MINOR` under ADR-0035.
* Tier configuration is snapshotted onto every trajectory (names, profiles, classifications,
  context budgets), so an explanation remains truthful after the operator edits a tier.
* Plan schema, approval-policy version and estimator version are recorded on every trajectory.
* LoadCoach API compatibility asserted by contract tests against LoadCoach's committed OpenAPI
  snapshot, not by a shared environment.
* Turn provenance, the explanation document and the fake LoadCoach carry **optional
  adapter-subject fields from birth** (absent unless an adapter served the turn), per the
  [adapter arc](../../roadmap/adapter-roadmap.md) LA0 — routed adapters within a tier later
  require no PromptCadence schema change.

## 20. Acceptance criteria

1. `pip install promptcadence && promptcadence serve` works with only LoadCoach + Ollama running; no cloud
   provider, no configuration beyond defaults.
2. `promptcadence run "summarize the files in ./notes"` plans, gets an automated approval, executes on a
   local tier with sandboxed tools, and returns a result plus a retrievable explanation naming
   every model, tier, tool call, debit and egress verdict.
3. The same trajectory with `--bypass-planning` produces a record identical in shape minus the
   plan and its approval rows — both runs execute every turn under an `ExecutionIntent`, differing
   only in how it was minted — demonstrated by the contract-1 diff test.
4. A trajectory declared `confidential` can never reach a remote tier: the attempt is refused
   before any HTTP request leaves, and the refusal is a queryable `EgressDecision`.
5. A remote tier with no pricing record refuses with `UNPRICED_EGRESS_REFUSED` before any call.

   Criteria 4 and 5 are properties of *when* a turn's pre-flights run, so the order is fixed by
   [ADR-0073](../../adr/0073-egress-is-decided-on-configuration-before-availability.md):
   **egress, then pricing, then availability, then budget**, all before the turn is announced and
   therefore before any request is built. Egress is first because it is the only unconditional
   one — a trajectory that may not use a tier may not use it whatever the price, the availability
   or the balance. It is decided on the tier's **configuration** and never gated on availability:
   otherwise a confidential trajectory aimed at a remote tier would be refused today for
   `loadcoach_has_no_remote_provider` and only for the real reason once LC-E1 registers a
   provider, and the recorded reason would have changed because infrastructure changed.
6. Crossing the money or token ceiling mid-trajectory halts (or pauses for approval) with the
   ledger showing every debit and the running balance that crossed.
7. With `approval.mode = "hybrid"`, a step requiring `internal` egress pauses the trajectory,
   appears in `promptcadence approvals list`, and a `deny` halts it with the denial recorded.
8. A deviation — the model requesting a tool outside its step's intent (`undeclared_tool`) —
   is handled per its category and `reapproval_scope`; a re-approval mints a superseding intent
   revision for that step only, visibly in the record with both revisions retained.
9. Kill −9 during an executing trajectory: recovery resumes or halts it cleanly per its lease,
   with no lost, duplicated or stuck trajectory and no orphaned LoadCoach job.
10. The full test suite passes with no LoadCoach, no GPU and no network.
11. All PromptCadence gold standards (added to [Gold Standards §2](../../standards/gold-standards.md) at
    Phase 0) are met.

## 21. Future extensions

* Remote-tier execution beyond a single OpenAI-compatible endpoint, when LoadCoach's
  multi-provider registration (LC-E1, [roadmap §5](../../roadmap/promptcadence-roadmap.md)) grows beyond
  it.
* A plan-quality benchmark category in FreeWeight (`native.plan`), so `tools.plan` routing gains
  measured evidence — deliberately not required for v1.
* Trajectory templates: parameterized, versioned plans for recurring tasks, skipping drafting but
  not approval.
* IdeaPress `research` stage executing as a PromptCadence trajectory — the ADR that adds a research
  backend decides it ([Workflows §2](../ideapress/workflows.md)).
* Multi-trajectory scheduling fairness (ageing, priorities) if concurrent operation grows past the
  single-operator default.
* A `PromptCadenceClient` package, at the same second-consumer trigger as `LoadCoachClient`
  ([ADR-0011](../../adr/0011-shared-package-boundaries.md)).
