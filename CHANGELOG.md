# Changelog

All notable changes to `promptcadence` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per packaging and release standards §3.

## [Unreleased]

### Added
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

