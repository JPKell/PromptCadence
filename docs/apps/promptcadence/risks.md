# PromptCadence — Risk and Failure Analysis

Suite-wide risks: [Risk Register](../../architecture/risk-register.md) (rows A9, A10, T12 name
PromptCadence directly).

---

## 1. Technical risks

| # | Risk | L | I | Mitigation | Early signal |
|---|---|---|---|---|---|
| T1 | **Governance bypassed structurally** — a code path executes a turn with no `ExecutionIntent` to check it against | Low | High | No such path exists by construction (spec §11 contract 1); a test diffs planned vs. bypassed record sets to prove both mint one | A turn row with no `intent_id` |
| T2 | **Bypass quietly removes more than planning** — the configurable bypass erodes into skipping budget, egress or the audit trail too | Low | High | The bypass removes only the up-front plan-and-approval round trip; every other check runs under a default `ExecutionIntent` either way (spec §1, [ADR-0048](../../adr/0048-the-bypass-removes-planning-never-governance.md)) | Bypassed and planned trajectories diverging in the record-diff test |
| T3 | **Governance overhead dominates**, creating pressure to bypass or thin it out | Medium | Medium | Spec §15 bounds PromptCadence's own overhead at ≤ 25 ms/turn beside multi-second model calls, performance-gated | `overhead_ms` trending up; a request to skip a ledger write "for speed" |
| T4 | **Explanation drifts from the rows it composes** — the materialized cache disagrees with a live recomposition | Low | High | `materialize(rows) == compose_live(rows)` is a golden byte equality (fixed key order, stored timestamps, no dict/set iteration dependence); the whole cache is droppable and re-read as a release check ([ADR-0093](../../adr/0093-materialization-follows-the-terminal-transition.md)) | The suite-wide delete-and-reread check failing |
| T5 | **Egress verified by assumption, not fact** — provider kind alone cannot tell a local llama.cpp server from a paid remote endpoint under `openai_compatible` | Medium | High | The egress class is resolved from the response's own declared `is_remote`/provider identity, never inferred from kind; absence is a violation, not a pass (spec §11 contract 4) | A `VIOLATION` `EgressDecision` with no clear cause |
| T6 | **Unpriced egress mistaken for free** | Low | High | A remote tier with no priced record is refused (`UNPRICED_EGRESS_REFUSED`), never billed as `$0.00`; local cost is `UNSUPPORTED`, never `$0.00` ([ADR-0016](../../adr/0016-unavailable-is-not-zero.md), [ADR-0030](../../adr/0030-model-cost-and-pricing.md)) | A ledger entry showing `$0.00` for priced usage |
| T7 | **A step advances on the model's prose** rather than a declared outcome | Low | High | Advance only on a declared `finish_reason` of `STOP` or a schema-validated structure; `LENGTH`/`ERROR`/absence handled explicitly (spec §11 contract 6) | A completed step whose `finish_reason` is absent or non-`STOP` |
| T8 | **Retry and escalation misordered** — a repeat fires after an escalation already claimed the approved tiers cannot serve, or vice versa | Medium | Medium | Fixed ladder: try each permitted tier, then a same-tier repeat under `step_retries`, then `tier_escalation`; a governance outcome (egress, pricing, budget, deviation) is never repeated (spec §13) | A `tier_escalation` request scoped to a step that never repeated once |
| T9 | **Reliance on a single-process, database-backed queue** — no broker, no horizontal scale | Medium | Medium | Deliberate ([ADR-0010](../../adr/0010-queue-implementation.md)); lease-based recovery proven at startup (≤ 2 s for 100 in-flight, spec §15) | Recovery time climbing past the ceiling |
| T10 | **Compaction loses the wrong turns**, or a summarization runs under a stale envelope | Low | High | Compaction is a view, never a deletion — every original turn stays in `turns`; a summary executes under a superseding intent revision, restored after ([ADR-0090](../../adr/0090-a-compaction-summary-runs-under-a-superseding-revision.md)) | `budget_unmet` on a compaction; a summary turn with no restoring revision |
| T11 | **A replayed tool call's arguments disagree with the record** | Low | Medium | A replayed call whose arguments exceed ToolYard's record bound is replayed with the record's size-and-digest object, never a silently truncated string ([ADR-0096](../../adr/0096-replayed-tool-call-arguments-are-capped-at-the-records-bound.md)) | `tool_call_records.args_json` and the wire disagreeing |

## 2. Integration risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| I1 | LoadCoach unreachable mid-trajectory | Medium | High | No direct-provider fallback (spec §3) — parks with a reason (`LOADCOACH_UNAVAILABLE`), health degrades, never a startup failure |
| I2 | LoadCoach's declared `finish_reason` absent (an older build) | Low | High | Treated as absence — a halt naming the gap, never a completion (spec §11 contract 6, the D2 gap this closed) |
| I3 | LoadCoach serves an API major this build does not speak | Low | Medium | `SCHEMA_VERSION_UNSUPPORTED` on first contact and every 5-minute recheck, never cached as working ([ADR-0013](../../adr/0013-api-versioning.md)) |
| I4 | Package sprawl — four new packages (CutCtx, ToolYard, LoadLedger, Commissioner) in one arc | Medium | Medium | Each built only with a named second consumer before it existed; `ThreadRack` was rejected on the same rule ([ADR-0011](../../adr/0011-shared-package-boundaries.md); risk register A9) |
| I5 | The mountable-tables pattern fights a host's Alembic autogenerate | Medium | Medium | Mount at module import, unconditionally, so autogenerate always sees the tables; PromptCadence is the pattern's first real host (risk register A10) |

## 3. Security risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| S1 | **Model output as an attack surface** — tool names, arguments and plan content are adversarial by assumption | Medium | High | Only registry-listed tools are callable regardless of what the model asks; arguments schema-validated then sandbox-checked; egress evaluated from the trajectory's own declared classification, never model text; no model output reaches a shell, path or fetch URL unchecked ([ADR-0095](../../adr/0095-the-injection-corpus-asserts-the-harness-never-the-model.md)) |
| S2 | Sandboxed tool execution escape | Low | High | Filesystem tools under per-trajectory workspace containment (symlinks resolved before checks); `run_command` under ToolYard's tiered isolation, container → bwrap → refuse |
| S3 | The operator console becomes a second, weaker auth surface | Low | High | Authenticates exactly as the API does, adds no session cookie; loopback-first by decision, not accident ([ADR-0094](../../adr/0094-the-console-authenticates-as-the-api-does.md)) |
| S4 | A submitter approves its own egress | Low | High | `approve` is its own scope, deliberately separate from `write` |
| S5 | Model-authored text becomes a template injection surface | Low | Medium | Autoescaping, `StrictUndefined`, no template renders raw HTML from a record |
| S6 | Transcript/tool text persisted past its useful life | Medium | Medium | Swept after `content_retention_hours` on a terminal trajectory; hashes, usage, decisions and events survive; in-flight work is never swept |
| S7 | LoadCoach API key exposure | Low | High | Read from an environment variable or file, never config plaintext, never logged, never in `details` |

## 4. Portability risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| P1 | `run_command` isolation unavailable on some platforms | Medium | Medium | Degrades through ToolYard's tiered table; refuses with a recorded reason rather than executing unisolated — never silently unsandboxed |

## 5. Performance risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| F1 | Explanation materialization slow on a large trajectory | Medium | Low | ≤ 2 s target / 10 s ceiling for 500 turns, performance-gated; the live-compose fallback carries the same budget as its own measured path |
| F2 | SSE overhead per event dominates a fast stream | Low | Low | ≤ 5 ms/20 ms budget; poll interval tuned from LoadCoach's own F12 measurement |
| F3 | Recovery of many in-flight trajectories slows a restart | Low | Medium | ≤ 2 s/10 s for 100 trajectories, performance-gated |

## 6. Model and provider risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| M1 | A tier's provider changes identity underneath a running trajectory | Low | High | Every response's execution subject is verified against the tier that requested it, not assumed (spec §11 contract 4) |
| M2 | Structured planning output unreliable | Medium | Medium | Bounded corrective retry, then `PLAN_DRAFT_FAILED` naming every attempt's issues — never an infinite retry |

## 7. Maintenance risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| G1 | Tier/task-profile sprawl | Medium | Medium | Tiers are configuration mapping to one LoadCoach task profile each; no routing math of PromptCadence's own (spec §3) |
| G2 | Explanation format churn breaking a consumer | Low | Medium | Application-owned document schema, versioned, additive-only in v1 (spec §9, [ADR-0035](../../adr/0035-application-owned-document-schemas.md)) |

---

## 8. Deliberate trade-offs

* **Planning is a model call PromptCadence pays for and can skip** — the bypass exists precisely
  because that round trip is the expensive, skippable half; governance is not.
* **Single machine, database-backed queue** — the LoadCoach precedent, no broker anywhere in the
  suite.
* **Explanations retained forever by default** — storage spent on the product's core promise.
* **A derived cache for explanations, never the source** — correctness is cheap to prove (delete
  and re-read) precisely because nothing depends on the cache being right.
* **No provider access of its own** — every generation is one more HTTP hop through LoadCoach, in
  exchange for a single, auditable egress path.

## 9. Explicit non-goals (restated as risk control)

Routing math, provider access, benchmark execution or evidence production, content-workflow
decisions, a standalone chat product, multi-machine execution. Each would duplicate another
component's responsibility (LoadCoach's routing, FreeWeight's evidence, IdeaPress's workflow
decisions) or reopen the ungoverned egress path the application exists to close (spec §3).

## 10. Premature optimizations to avoid

* A learned or adaptive planner before the bounded corrective retry is proven insufficient.
* A cache for routing or tier decisions before the per-turn overhead budget is missed.
* Extracting `ThreadRack` before a second consumer of thread state exists.
* Parallel step execution before the DAG scheduler's sequential path is proven correct.

## 11. Architectural traps

* Letting the bypass erode into skipping budget, egress or the audit trail — it removes planning,
  never governance (§1 T2).
* Deriving egress class from provider *kind* instead of verifying the response's own declared
  identity — `openai_compatible` covers both a local server and a paid endpoint.
* Treating the materialized explanation as the source of truth instead of a cache that must survive
  being deleted.
* Repeating a governance outcome (an egress denial, a budget ceiling) the way a transient provider
  failure is repeated.
* Reaching FreeWeight, LoadCoach or IdeaPress as code instead of HTTP with versioned payloads.
