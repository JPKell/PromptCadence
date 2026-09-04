# Changelog

All notable changes to `promptcadence` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per packaging and release standards §3.

## [Unreleased]

### Added
- **Phase 5, gate A: LoadLedger's tables are mounted.** `loadledger[sql]>=0.1,<0.2` is a runtime
  dependency, and this is the **first package-table mount in the application** (ADR-0050) — the
  example Commissioner's `egress_decisions` mount at Phase 6 and CutCtx's later one copy.
  - `infrastructure/db/models.py` — `LEDGER_TABLES = mount_ledger_tables(Base.metadata)` at module
    import, unconditionally and behind no flag: autogenerate only sees what was mounted before it
    inspected the metadata, so a lazy mount produces a revision that *drops* the package's tables.
    The prefix stays the package default `ledger_`, which is part of the mounted contract —
    changing it after a deployment has migrated is a table rename, not a setting.
  - Migration `0005` creates `ledger_entries`, `ledger_balances`, `ledger_balance_money` and
    `ledger_runs`; a test proves `alembic upgrade head` from empty produces exactly the mounted
    shapes, and that `downgrade` removes them and nothing else. The `[sql]` extra is what is
    needed and not the bare package: LoadLedger keeps SQLAlchemy behind an extra by decision
    (ADR-0050 decision 4), and a host that mounts tables needs that half.
  - `trajectories` gains the persisted `awaiting_window` clock (`window_parked_from`,
    `window_next_edge_at`, `window_days_waited` — `domain.trajectory.WindowWait` field for field)
    and `budget_partial_pricing`, the per-request override of `[budget] partial_pricing`.

- **Phase 5, gate B: the ceilings bind.** `declare_run` fires at trajectory creation, in the same
  write that persists the row and before plan approval, so no pre-flight ever meets `UnknownRun`.
  - `services/budget.py` — the application's half of the budget: which ceilings are active
    (per-trajectory, per-day, per-project — up to three at once, the most restrictive binding),
    the debit, the pre-flight and the adapter from `loadledger.CeilingVerdict` to `domain.policy.
    BudgetHeadroom`. No arithmetic of its own: LoadLedger keeps the balances and decides every
    `exceeded`.
  - `services/pricing.py` — a tier's `pricing_file`, read once at startup into
    `baseaicore.ModelPricing`. PromptCadence is the suite's first consumer of that type, so the
    file format is defined here: JSON with a `records` array, rates as decimal **strings**, and an
    omitted rate meaning "not stated" rather than free. An unreadable file refuses at startup.
  - `services/estimates.py` — the layered estimator over `entries()`: historical p80 per token
    class at or above `estimate_min_samples`, else the tier's configured per-step default, with
    the source recorded either way. A model-generated number is never an input (ADR-0047 / D-3).
  - Debits carry `tier:<name>` and `project:<name>`, rebuild `TokenUsage` from all four classes
    (ADR-0070), and store usage plus a `pricing_hash` and never a money figure (ADR-0030).
  - Exhaustion routes per ceiling: `on_exhausted` halts or parks on one pending approval request
    (T10 — granting the raise stays P7's), and `on_daily_exhausted = "window"` parks in
    `awaiting_window` with the persisted clock, resuming on the UTC day edge (T16), staying parked
    when the new day is already spent, and halting after `window_wait_max_days` (T17).
  - **Money ceilings bind priced usage only** (ADR-0047 §3). A money cap that someone else's
    priced work exhausted does not stop a local step, whose cost is `UNSUPPORTED` and which cannot
    add a nano to it; the token ceiling is the universal brake and binds every step on every tier.
  - `[tiers.<name>]` gains `default_step_input_tokens` and `default_step_output_tokens` — the
    estimator's `configured_default` rung, which lifecycle §6 names and configuration had no field
    for. Two numbers rather than one total, because the classes price differently.
  - `POST /trajectories` accepts `partial_pricing` as a per-request override of `[budget]
    partial_pricing`, persisted on the row; `NULL` means "the configured default", which is not
    the same as either value written down.

- **Phase 5, gate C: a partial price is a floor, and a money ceiling chooses how it binds**
  (ADR-0069). Under `floor` — the default — the priced components accumulate and the trajectory
  continues, so the brake can fire late by the unreported portion and never early. Under `strict`
  a window holding an estimate that did not total **exceeds** its money ceiling, and the refusal
  lands at pre-flight — before the call, not as a verdict recorded after it.
  - `render_money` / `render_tokens` in `services/budget.py` are the one renderer every surface
    goes through, so the API, the CLI and every cause string cannot disagree about what a floor
    looks like: `at least 0.004 USD`, never a bare figure, and `—` for an unpriced amount — never
    `$0.00`, which would say the work was free (ADR-0016, spec §20 criterion 1).
  - A local step trips neither rule: a debit that carried no estimate at all is *unpriced* and not
    *untotalled*, which is what keeps a mixed trajectory running under `strict`.

### Fixed
- **`trajectories.budget_money_nanos` and `budget_token_ceiling` were `Integer` and are now
  `BigInteger`** (migration `0005`). `Money` is whole nanos, so the shipped $5.00 default ceiling
  is 5 000 000 000 — past a 4-byte integer's 2 147 483 647. SQLite's dynamic typing stores it
  regardless, so no SQLite test could have caught it; on PostgreSQL every trajectory carrying a
  money ceiling above $2.14 would have failed to insert. Widened in the revision that first makes
  those columns bind anything.

### Added
- **Phase 4: the loop executes tool calls under full ToolYard discipline.** `toolyard>=0.1,<0.2`
  is a runtime dependency now that `0.1.0` is on PyPI, and the Phase-3 placeholder — *"tool calls
  are not executed before Phase 4, and a requested tool that cannot run is not a completed turn"* —
  is replaced by the round trip the plan specifies: `tool_calls` → ToolYard → results appended as
  **TOOL** turns → continue until a declared finish, bounded by `execution.max_turns_per_step`.
  The placeholder's surviving claim is kept: a turn that requested tools is still not a completed
  turn.
  - `services/tools.py` — the assembly. **One** `TieredSandbox` per process, handed to
    `run_command_tool` before it is registered, so the tier the executor checks and the rung the
    command runs under are one answer to one question; one registry, shared, because what narrows
    per trajectory is the *allowlist* and not the registry; one absolute workspace per trajectory
    under `[tools] workspace_root`; and an artifact store keyed by the digest of the whole output.
    Startup refuses a relative root, a read root overlapping the workspace root, and a
    `process_count` below ToolYard's documented floor.
  - **`http_fetch` is withheld, not disabled.** No tool performs network egress before Phase 6, and
    rather than editing spec §12's shipped `[tools] enabled`, the tool is simply not registered —
    with a cause (`egress_governance_deferred_to_p6`) that `GET /tools`, `promptcadence tools list`
    and `doctor` each show. A model that asks for it is refused with `unknown_tool`, which is true,
    and the refusal is recorded. Two independent facts keep it off the network: it is not in the
    registry, and every invocation's egress ceiling is `none`.
  - **A refused call is a result, not a halt.** `Disposition.REFUSED_NOT_REAPPROVABLE` joins
    `CONTINUE_RECORDED` in the set that continues a trajectory. Lifecycle §5 says a tool outside
    the *trajectory* allowlist has **the call** refused outright and recorded, never re-approvable,
    and says nothing about the trajectory ending; before tools could run, a halt was the only
    available reading of that cell. Scoped re-approval still halts — the approver arrives at P7.
  - `infrastructure/loadcoach.py` gains `assemble_tool_calls`: LoadCoach forwards tool calls as the
    provider streamed them (`call_index`, `id`, `name`, `arguments_fragment`), so one call can be
    three entries. They are grouped and parsed **once** before anything counts a name or executes
    anything. It refuses nothing — a parser that raised on model output would let the model choose
    when a turn ends.
  - `tool_call_records` and migration `0004`, one row per call **including refusals and failures**;
    `turns.tool_call_id`, closing a gap the domain has carried since Phase 2; `tool.call.started`
    and `tool.call.completed` on the event stream, carrying digests and never arguments or output;
    and oversize output spilled to the artifact directory **by hash**, with the record naming the
    digest. Nothing is ever filed when the content's digest does not match the record's — a prefix
    under the whole output's hash is the truncated body pretending to be complete that the record
    exists to prevent.
  - `GET /tools`, `GET /tools/{name}`, and `promptcadence tools list|show` (mode: local — the
    registry is a function of configuration *and of this host*, so the useful answer comes from
    probing the machine the command runs on). A third health component, `tools`, carries ToolYard's
    `TierReport.reason`, so an operator can see which rung the ladder landed on and why without
    reading logs. It is never `unavailable`: a host with no isolation rung still runs every
    filesystem tool.
  - `[tools]` gains `artifact_root`, `container_image`, `max_result_chars` and `timeout_seconds`.
  - `tests/contract/loadcoach_task_profiles.toml` vendors LoadCoach's shipped profiles at
    `5c5aa1f` with its digest recorded, and a contract test asserts the pairing every tier depends
    on: the profile exists, both agent profiles require `tool_use`, each profile's
    `min_context_tokens` equals its tier's `context_budget_tokens` and its `allow_remote_providers`
    equals the tier's `remote`, and `tools.plan` asks for JSON with no schema. A drift now fails in
    CI rather than at an operator's `tiers check`. The fake LoadCoach stays an **empty registry**
    and gains `shipped_profiles()`, so a test registers the profile LoadCoach actually ships.
  - Tests: spec §18's five security rows at this layer, each on its own — unlisted tool, path
    escape, symlink escape, a refusal fed back as a structured TOOL turn with the trajectory
    continuing, and size caps — a scripted multi-tool journey, and the hostile scripted model of
    acceptance criterion 1, which requests unlisted tools, escapes paths, sends arguments that are
    not JSON and asks for a command on a host with no isolation rung, and must reach a terminal
    state with **every** call recorded. That no exception crossed the loop is asserted by a trap
    around `run`, not observed. The default suite still passes with no LoadCoach, no Ollama, no GPU
    and no network.

  No version bump; this rides `0.9.0b0` at M11.
- Phase 3: a bypassed trajectory executes end to end — durably, observably, and under
  governance — against a fake LoadCoach that every later phase tests against.
  - `infrastructure/loadcoach.py`: the httpx client for `/version`, `/system/status`, `/models`,
    `/task-profiles`, `/route`, `/generate`, `/jobs` and `/jobs/{id}/cancel`, with every
    LoadCoach error code mapped onto exactly one spec §13 code and the original preserved in
    `details` (never `INTERNAL_ERROR`). Both `usage` wires are read — the interim one with the
    cache classes `"unsupported"`, and the post-`modelrack 0.7.0` one with `0` or a count — and
    the three answers never render as one another (ADR-0016, ADR-0070). Every request carries
    `X-Client-Name: promptcadence` and every turn's `idempotency_key` is its turn id.
  - `services/loop.py`: the bypass `LoopController` — claim (T3), mint the default
    `ExecutionIntent`, one turn per `/generate`, the turn row with `turn.completed`, every
    deviation as a row and an event, and the terminal transition, in one write (ADR-0044).
    A turn completes only on a declared `finish_reason=stop` or a schema-validated result
    (`domain/turns.py`); `length`, `error`, an undeclared reason and absence halt naming the
    cause. The declared reason is read from `output.finish_reason`, which LoadCoach renders
    since `846348b` (the gap D2 found and LoadCoach closed in the same row); an
    older LoadCoach's wire carries none, and a free-text tier halts on it rather than
    completing. Every response's
    execution subject is verified against the provider surface read from `/models`
    (`services/loadcoach_surface.py`), and a foreign provider on a local tier halts as a
    `tier_violation`.
  - `services/worker.py`: the worker pool, the lease keeper (renewal by compare-and-set; a
    lost lease fences every write) and the recovery pass of lifecycle §8.3 — an in-flight
    LoadCoach job found by its idempotency key is cancelled, a completed one is reconciled into
    the turn row, an unreconcilable one halts `recovered_after_crash`, an unreachable LoadCoach
    defers. Proven with a real `kill -9` at both places a turn can be lost.
  - `services/events.py`: the ADR-0044 sink — a state change and its event commit in one
    transaction and publish only after it; gap-free per-trajectory sequences for SSE replay.
  - `POST /trajectories`, `GET /trajectories(/{id})`, `GET /trajectories/{id}/turns`,
    `POST /trajectories/{id}/cancel` (at once when unleased, at the next turn boundary when
    leased, with the in-flight LoadCoach job cancelled) and the SSE `GET /trajectories/{id}/stream`
    with `Last-Event-ID` replay; `promptcadence run [--follow]` and
    `promptcadence trajectory list|show|cancel|wait`, with CLI standards §4 exit codes.
  - `tests/fakes/loadcoach_app.py`: the fake LoadCoach, speaking exactly the documented wire
    (`output.finish_reason` and the job document's validation `checks` as LoadCoach
    `846348b` renders them, and the older wire on request) and stricter than the real
    thing where recovery depends on it; `tests/contract/`: the I10 contract tests against LoadCoach's
    committed OpenAPI snapshot; `tests/live/`: the marked live journey against a real LoadCoach.
  - Migration `0003`: `lease_owner`, `lease_expires_at`, `cancel_requested` and `error_code` on
    `trajectories`.
- Phase 2: the domain core — every governance decision that needs no I/O, as a pure,
  golden-tested function.
  - `domain/intent.py`: the immutable, revisioned `ExecutionIntent` (ADR-0056) with its three
    minting paths — an approved plan step, the bypass default, and supersession — and no fourth.
    A turn's `TurnProvenance` takes the intent as a `dataclasses.InitVar`, so a turn cannot be
    constructed without the envelope it ran under: spec §11 contract 1 is structural, not a rule
    to remember. Gates are evaluated at minting against the most permissive tier the intent
    permits, so a pre-approved fallback cannot smuggle egress past a hybrid gate.
  - `domain/deviation.py`: `compare(turn_facts, intent)`, identical in both paths, over the six
    categories of lifecycle §5. `TurnFacts` is a closed shape carrying no trajectory-level
    ceiling, which is the other half of the taxonomy's closure; severity is derived from the
    category rather than stored beside it.
  - `domain/tiers.py`: admission (`classification ≤ ceiling` over `baseaicore.DataClassification`),
    explicit escalation, remote-tier availability, and the content-addressed `TierSnapshot` a
    trajectory records so its explanation survives a configuration change.
  - `domain/plan.py`: the committed plan JSON Schema (mirrored byte-identically by
    `plan.schema.json`) and lifecycle §4.1's five rules — DAG, no classification laundering,
    declared tools and tiers, non-empty — reporting every issue at once, each naming its step and
    field.
  - `domain/policy.py`: the automatic approval verdict over tier policy, egress policy and ledger
    headroom, with `approval_policy_version` **derived** from a digest of the configured values
    and of the ruleset's own decisions, so changing a rule cannot leave the version unchanged.
    `BudgetHeadroom` mirrors LoadLedger's `CeilingVerdict` including its three honesty counts.
  - `domain/trajectory.py`: lifecycle §8.2's T1–T17 as explicit functions with their guards
    implemented, every unlisted transition refused, and terminal states absorbing by construction.
  - `domain/threads.py`: package-shaped `Thread`, `Turn` (generic over its provenance),
    `ThreadSnapshot` and the `ThreadStore` port; `infrastructure/threads.py` holds the SQLAlchemy
    implementation, since `domain` may not import it.
  - `domain/events.py` and `domain/errors.py`: spec §17's and spec §13's closed vocabularies, with
    event bodies beside the code that mints them and the rule that a body carries ids, categories
    and numbers — never prompt text, never model output.
  - `services/policy_assembly.py`: the one place validated `Settings` becomes domain values.
  - Migration `0002`: `tier_snapshots`, `plans`, `plan_steps`, `plan_approvals`,
    `approval_requests`, `execution_intents` and `deviations`, plus `(intent_id, intent_revision)`
    and the two cache token classes on `turns` (ADR-0070 decision 7) and
    `tier_snapshot_id` / `approval_policy_version` on `trajectories`. Born with the domain types
    that define them rather than retrofitted by the loop that first writes them.
- Phase 1: skeleton, configuration, database and honest-degradation health.
  - `config.py`: the full spec §12 surface (server, storage, loadcoach, planning, approval,
    execution, budget incl. `[budget.projects.*]`, tools, compaction, `[tiers.*]`, policy,
    logging) with the full file -> environment -> CLI precedence chain, and every startup
    refusal from spec §12 and ADR-0026/ADR-0047/ADR-0049.
  - `infrastructure/db/models.py` and migration `0001`: `trajectories`, `threads`, `turns`,
    `events`, `api_tokens`, `settings`, on `weightsdb`. `turns` carries the optional LA0 adapter
    fields from birth (adapter-roadmap §4.5).
  - `web/app.py`: MirrorWall's request-ID and Host-validation middleware and the standard error
    envelope; `GET /api/v1/health`, `GET /api/v1/version`, `GET /api/v1/system/status`.
  - `cli/`: `serve`, `health`, `doctor`, `version`, `config show|validate|init|path`,
    `db upgrade|status|backup|restore`.
  - `.importlinter`: the standard `web`/`cli`/`services`/`domain` layering, plus a dedicated
    contract forbidding `modelrack` and `sweatmeter` imports anywhere in the package
    (ADR-0045 rule 2) — written now so no later phase can add either import casually.

### Changed
- `[dev]` pins `pytest>=9.0.3,<10`, excluding PYSEC-2026-1845 and matching every sibling
  repository; CI's `security` job runs `pip-audit` against the locks.

