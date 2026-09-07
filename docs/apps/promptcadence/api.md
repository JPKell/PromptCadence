# PromptCadence — Public API

**Base path:** `/api/v1` · **Conventions:** [API and Contract Standards](../../standards/api-and-contract-standards.md)
Derived from the committed OpenAPI snapshot (`PromptCadence/docs/openapi.json`, byte-tested against
the live schema) and the route modules under `web/routes/`. Every path in that snapshot is covered
below.

---

## 1. System

| Endpoint | Purpose |
|---|---|
| `GET /health` | MirrorWall's standard health payload over `database` and `loadcoach`. `200` when every component is `ok` or `degraded`, `503` when any is `unavailable` — an unreachable LoadCoach makes this endpoint **degraded**, never `unavailable` (spec §20 AC1), so only an unopenable database can drop it below 200. `read` scope |
| `GET /version` | `{"application": "promptcadence", "version": "…", "api_version": "v1", "schema_version": "1"}`. **Never authenticated** — negotiation precedes credentials ([ADR-0026 §5](../../adr/0026-local-http-hardening.md)) |
| `GET /system/status` | The operator dashboard (spec §17): active trajectories (`planning`, `executing`), every pending approval request with its age, today's ledger position against the per-day ceiling and each configured project's, the last recovery pass, and the configured concurrency. `read` scope |

## 2. Tiers and tools

| Endpoint | Notes |
|---|---|
| `GET /tiers` | Every configured tier with its ceilings and availability. A remote tier is available only when LoadCoach has a registration declaring `remote = true` **and** the tier is priced; an unavailable one names which of the two is unmet ([ADR-0098](../../adr/0098-promptcadence-1-0-ships-with-remote-tiers-refusing-honestly.md) rule 3). `read` scope |
| `GET /tools` | `{"tools": [...], "isolation": {...}}` — every tool `[tools] enabled` names, registered or withheld, each with its risk class, egress class, whether it needs isolation, whether its arguments are redacted, its argument schema, and — for a withheld one — the cause. `isolation` is ToolYard's probe result: the rung `run_command` runs under, the runtime, and the reason naming every rung the probe visited. `read` scope |
| `GET /tools/{name}` | One tool by exact name. `422 TOOL_NOT_FOUND` — not `404` — for an unconfigured name: spec §13's table assigns that code one status, and the submit path needs it there too. A **withheld** tool (configured but not registered) is found, not missing |

## 3. Trajectories

### `POST /trajectories`

```json
{
  "task": "Summarize the open issues in the billing service and draft a fix plan.",
  "data_classification": "confidential",
  "budget": {"tokens": 200000, "money": {"currency": "USD", "nanos": 5000000000},
             "partial_pricing": null},
  "project": null,
  "tools": ["list_dir", "read_file"],
  "bypass_planning": null,
  "tier": null,
  "max_steps": null,
  "max_turns": null
}
```

`task` is the only required field (`minLength: 1`). `data_classification` defaults to
`"confidential"` — unclassified data is treated as most restrictive
([ADR-0046](../../adr/0046-data-classification-is-ordered-and-defaults-closed.md) rule 3) — and is a
**string**, not an enum, so a value outside the three levels is refused as `CLASSIFICATION_INVALID`
(spec §13), a code a caller can act on, rather than a generic validation error. `budget.tokens` and
`budget.money` are each optional and independent — a request may set either, both or neither, over
`{currency, nanos}`; `budget.partial_pricing` is **three-valued**: absent means "the configured
default" (`[budget] partial_pricing`), which is not the same as either `"floor"` or `"strict"`
pinned — a request that pinned the current default still pinned it, and a later configuration
change must not silently move it ([ADR-0069](../../adr/0069-a-partial-price-is-a-floor-and-a-money-ceiling-chooses-how-it-binds.md)).
`project` must name a configured `[budget.projects.<name>]`, else `PROJECT_UNKNOWN`; every debit
from a labelled trajectory is tagged `project:<name>` and the project's ceiling binds beside the
trajectory's own and the shared per-day one. `tools` is an allowlist checked against the registry
(`TOOL_NOT_FOUND` for an unknown name); `tier` pins a tier as an override (policy still applies, and
an unconfigured name is `TIER_NOT_CONFIGURED`); `max_steps`/`max_turns` bound the trajectory within
configured caps. Every body field forbids unknown properties (`additionalProperties: false`).

Errors: `VALIDATION_ERROR`, `CLASSIFICATION_INVALID`, `PROJECT_UNKNOWN`, `TOOL_NOT_FOUND`,
`TIER_NOT_CONFIGURED`. `write` scope. Response `202`, the trajectory document (below).

### `GET /trajectories`

Newest first, filtered by `?state=`, cursor-paginated (`?limit=`, `?cursor=`; limit clamped to 200 —
API standards §6). `read` scope.

### `GET /trajectories/{trajectory_id}`

The trajectory document. `404 TRAJECTORY_NOT_FOUND` for an unknown id. `read` scope.

```json
{
  "trajectory_id": "01J9K…",
  "task": "Summarize the open issues in the billing service and draft a fix plan.",
  "state": "executing",
  "data_classification": "confidential",
  "project": null,
  "tools": ["list_dir", "read_file"],
  "bypass_planning": false,
  "tier": null,
  "max_turns": null,
  "max_steps": null,
  "budget": {"tokens": 200000, "money": {"currency": "USD", "nanos": 5000000000},
             "partial_pricing": null},
  "window_wait": null,
  "tier_snapshot_id": "3f2a…",
  "approval_policy_version": "2026-08-01",
  "cause": null,
  "error_code": null,
  "cancel_requested": false,
  "lease": {"owner": "worker-1", "expires_at": "2026-09-07T12:03:00Z"},
  "created_at": "2026-09-07T12:00:00Z",
  "updated_at": "2026-09-07T12:02:00Z",
  "completed_at": null
}
```

`cause` is the verbatim halt or failure reason (`null` while running); `error_code` is the spec §13
code beside it once the trajectory has one. `window_wait` is non-`null` only while the trajectory is
parked at `awaiting_window` (`on_daily_exhausted = "window"`): `parked_from` (the state it will
resume to), `next_edge_at` (the next UTC day boundary) and `days_waited`. `budget.partial_pricing`
carries the same three-valued reading as the request body — `null` here means the trajectory is
running under the configured default, not that no rule applies.

### `GET /trajectories/{trajectory_id}/turns`

Every turn in order, each with its `(intent_id, revision)` and LoadCoach job. `read` scope.

```json
{
  "turn_id": "01J9K…", "step_id": "loop", "sequence": 3, "role": "assistant",
  "content": "…", "content_sha256": "…",
  "tier": "local_fast", "intent_id": "01J9K…", "intent_revision": 1,
  "model_canonical_id": "ollama/qwen3.5:9b-q8_0@sha256:1f3a9c4e2b70",
  "finish_reason": "tool_calls",
  "usage": {"input_tokens": 812, "output_tokens": 140,
            "cache_write_tokens": 0, "cache_read_tokens": 128},
  "loadcoach_job_id": "01J9K…", "loadcoach_ms": 640.2, "overhead_ms": 4.1,
  "prompt_id": null, "prompt_version": null, "prompt_sha256": null,
  "created_at": "2026-09-07T12:00:12Z"
}
```

A token class follows LoadCoach's own rule: a real count, or the string `"unsupported"` — never a
number standing in for an unreported class
([ADR-0016](../../adr/0016-unavailable-is-not-zero.md), [ADR-0070](../../adr/0070-an-absent-token-class-is-zero-only-where-the-protocol-cannot-bill-it.md)).
`prompt_id`/`prompt_version`/`prompt_sha256` are set only on the step-framing turn a planned step
opens with — the record of PromptCadence's *own* prompt, never the caller's or the model's (spec §9);
every other turn carries `null` there. `step_id` names the step whose thread the turn belongs to
(`"loop"` on the bypass path — one shape in both modes, never a nullable column every reader must
branch on).

### `GET /trajectories/{trajectory_id}/plan`

Every drafting attempt (valid or not, with the planning call's subject, token classes and prompt
record), the validated steps with their execution state, and the recorded verdict. `null` for a
bypassed trajectory or one not yet drafted; `404 TRAJECTORY_NOT_FOUND` for an unknown one. This is
the plan rows rendered — the composed explanation document below is a different, larger read.
`read` scope.

### `GET /trajectories/{trajectory_id}/intents`

Every `ExecutionIntent` revision the trajectory minted, superseded ones included — append-only,
keyed on `(intent_id, revision)`. `read` scope.

### `GET /trajectories/{trajectory_id}/explanation`

The full reconstructable record (spec §11 contract 2): plan (when planned), approvals, every intent
revision, every turn with its intent reference and LoadCoach explanation reference, every tool call,
every ledger entry, every egress decision, in turn order.

Served from the materialized revision for a terminal trajectory and composed live for an in-flight
one — and also composed live for a terminal one whose revision has not been written yet or was
dropped, which is an ordinary state rather than an error
([ADR-0093](../../adr/0093-materialization-follows-the-terminal-transition.md) rule 3). The
response headers say which path answered, because the two carry different spec §15 budgets (≤ 25 ms
materialized, ≤ 2 s composed) and because **the body is the document** — a "which cache answered"
field folded into it would make two reads of the same rows differ:

* `X-Explanation-Source` — `materialized` or `composed`.
* `X-Explanation-Composed-Ms` — how long this read took.
* `X-Explanation-Revision`, `X-Explanation-Revision-Cause` — only when a materialized revision
  answered.
* `ETag` — the document's own SHA-256, quoted, only when a materialized revision answered.

`read` scope. `404 TRAJECTORY_NOT_FOUND` for an unknown trajectory.

### `POST /trajectories/{trajectory_id}/cancel`

T14: at once for an unleased trajectory, at the next turn boundary for a leased one — cancellation
is honoured at the turn boundary and cancels any in-flight LoadCoach job. `202` with the trajectory
document. `409 TRAJECTORY_NOT_CANCELLABLE` for one already terminal. `write` scope.

### `GET /trajectories/{trajectory_id}/stream`

SSE per [API Standards §8](../../standards/api-and-contract-standards.md): every frame a persisted
event, replay from `Last-Event-ID`. The stream closes on the terminal event. `404` before any frame
for an unknown trajectory. `read` scope.

## 4. Approvals

### `GET /approvals`

Pending requests, oldest first; `?trajectory_id=` narrows, `?status=all` includes resolved ones
(`?status=<other>` filters to that status). Each item names the trajectory, `kind` (`plan`,
`gated_step`, `bypass_gate`, `reapproval`, `ceiling_raise`), the steps it is scoped to, what it asks
(`detail`), when it expires and `age_seconds`. `read` scope.

```json
{
  "request_id": "01J9K…", "trajectory_id": "01J9K…", "kind": "reapproval",
  "status": "pending", "reason": "tier_escalation", "step_ids": ["s2"],
  "detail": {"category": "tier_escalation", "step_id": "s2", "intent_id": "01J9K…",
             "intent_revision": 1, "next_tier": "remote_frontier"},
  "created_at": "2026-09-07T12:00:00Z", "expires_at": "2026-09-07T12:15:00Z",
  "resolved_at": null, "approver_token_id": null, "resolution_reason": null,
  "age_seconds": 42.5
}
```

### `POST /trajectories/{trajectory_id}/approve`

T8: mint what the pending request asked for, under the caller's identity. Requires the `approve`
scope. Idempotent per request: granting an already-granted request returns `200` with
`already_resolved: true` and changes nothing. Body is optional; `budget` (a `{tokens, money}` raise)
is **required** for a `ceiling_raise` request and **refused** (`VALIDATION_ERROR`) for any other
kind.

```json
{
  "trajectory_id": "01J9K…", "state": "executing", "already_resolved": false,
  "request": { "...": "the ApprovalRequestView document above" },
  "minted": [{"intent_id": "01J9K…", "revision": 2, "step_id": "s2"}]
}
```

`409 APPROVAL_INVALID_STATE` when nothing is pending or the last request was denied or expired;
`400 VALIDATION_ERROR` when a ceiling raise offers no budget or a budget is offered to a request
that is not a raise.

### `POST /trajectories/{trajectory_id}/deny`

T9: halt the trajectory with the denial recorded, under the caller's identity. Requires the
`approve` scope. Body is optional (`{"reason": "…"}`, max 2000 characters). Idempotent per request.
`409 APPROVAL_INVALID_STATE` when nothing is pending and the last request was not denied.

```json
{
  "trajectory_id": "01J9K…",
  "request": { "...": "the ApprovalRequestView document above, now denied" },
  "state": "failed"
}
```

## 5. Ledger

| Endpoint | Notes |
|---|---|
| `GET /ledger` | Today's position against the per-day ceiling and each configured project's; `?trajectory_id=` adds that trajectory's own per-run position (`404 TRAJECTORY_NOT_FOUND` if it names one that does not exist). `read` scope |
| `GET /ledger/entries` | Recorded debits, newest first; `?trajectory_id=`, `?tag=`, `?limit=` (clamped to 200). Each entry carries its four token counts, its `pricing_hash` and every ceiling's verdict as of that debit — never a money figure as a fact of its own ([ADR-0030](../../adr/0030-model-cost-and-pricing.md) rule 1). `read` scope |

`GET /ledger` response:

```json
{
  "as_of": "2026-09-07T12:00:00Z", "utc_day": "2026-09-07",
  "day": {"scope": "day", "tokens_remaining": 850000, "binds": false, "..." : "..."},
  "projects": [{"project": "billing", "scope": "project", "..." : "..."}],
  "trajectory": null,
  "tiers": [{"tier": "local_fast", "tokens_spent": 12000,
             "tokens_spent_display": "12,000", "money_spent": [],
             "money_spent_display": "not priced"}]
}
```

`tiers` is **spend**, not headroom: no tier ceiling is configured, so there is no `remaining` and
nothing to `exceed` — inventing either would invent a cap that does not exist. A local tier's money
is `[]` and its display string says `"not priced"`, never `"$0.00"`
([ADR-0016](../../adr/0016-unavailable-is-not-zero.md)). Every money figure crosses the wire twice:
once as `{currency, nanos}` for a caller that computes, and once as a rendered `display` string
(`"at least …"` for a floor, `"at most …"` for remaining headroom) for a caller that shows it.

## 6. Egress

### `GET /egress-decisions`

Recorded egress decisions, oldest-decided first; `?trajectory_id=`, `?verdict=` (`approved`,
`denied` or `violation` — `400 VALIDATION_ERROR` outside that vocabulary), `?target=` (a tier or
host name), `?limit=` (clamped to 200). Every decision this build made is here — approvals, denials
and the violations a verification step wrote after the fact — each rendered as SetSpec's
`governance.egress_decision` 1.0, built from Commissioner's own `to_payload()` rather than a
hand-written projection, so the wire shape cannot drift from the package's
([ADR-0051 §4](../../adr/0051-plans-stay-internal-and-one-payload-travels.md)). `source_ref` names the turn or
tool invocation the decision gated. `read` scope.

## 7. Settings

`GET /settings`, `PUT /settings` — runtime-changeable settings only (spec §12,
[ADR-0100](../../adr/0100-promptcadences-runtime-changeable-set-is-five-tuning-numbers.md)).

`GET` (`read` scope) returns every runtime-changeable key's effective value, its type, bounds,
description, the configured value, the stored row (or `null`), which of the two is in force, and —
when the environment pins the key — what shadows the row, plus the list of config-only key names.
A stored value that does nothing is visible as such rather than silently applied or silently
dropped (configuration standards §7). `PUT` (`admin` scope) takes `{key: value, …}` for one or more
runtime-changeable keys and answers with the same document. A security-relevant key is
`403 FORBIDDEN` **naming the key** and the request is refused whole (nothing is written); an unknown
key is `400 VALIDATION_ERROR` naming it and listing the runtime-changeable set; a value of the
wrong type or outside its bounds is `400 VALIDATION_ERROR`. A change is applied by the running
worker at its next lease reap.

## 8. Errors

Standard envelope (`code`, `message`, `request_id`, `details`). Every code below is one a built
phase can actually raise (`promptcadence.domain.errors.ErrorCode`); a spec §13 code with no
exception class yet — `PLAN_REJECTED`, `APPROVAL_REQUIRED`, `STEP_LIMIT_EXCEEDED`,
`BUDGET_EXCEEDED`, `TOKEN_BUDGET_EXCEEDED`, `EGRESS_DENIED`, `TOOL_ARGS_INVALID`, `TOOL_REFUSED`,
`TOOL_EXECUTION_FAILED` — is reserved for the phase that introduces it and not yet reachable.

| Code | Status | Notes |
|---|---|---|
| `VALIDATION_ERROR` | 400 | Request body failed schema validation; `details.fields` lists `{"path", "problem"}` |
| `CLASSIFICATION_INVALID` | 400 | `data_classification` is not a `DataClassification` member |
| `SCHEMA_VERSION_UNSUPPORTED` | 400 | LoadCoach serves no API major this build speaks — a `LoadCoachError` subclass, deliberately, so it halts a trajectory exactly where any other LoadCoach failure does ([ADR-0013](../../adr/0013-api-versioning.md)) |
| `TRAJECTORY_NOT_FOUND` | 404 | No trajectory has that id |
| `TOKEN_NOT_FOUND` | 404 | No API token has that id (token management) |
| `PROJECT_UNKNOWN`, `TOOL_NOT_FOUND`, `TIER_NOT_CONFIGURED`, `COMPACTION_FAILED` | 422 | A request named something configuration does not define |
| `TRAJECTORY_NOT_CANCELLABLE`, `APPROVAL_INVALID_STATE` | 409 | A state transition was refused; `IllegalTransitionError` (internal, not a spec §13 code) is mapped onto one of these before it reaches an envelope |
| `TIER_UNAVAILABLE` | 503 | The tier is configured here and cannot be served there — `details` preserves every candidate and its rejection reason exactly as LoadCoach reported them |
| `LOADCOACH_UNAVAILABLE` | 503 | LoadCoach could not be reached at all (connection refused, DNS failure, a dropped socket) — distinct from `LOADCOACH_ERROR`, which is LoadCoach *answering* with a failure |
| `LOADCOACH_ERROR` | 502 | LoadCoach answered with an error, or a response this build cannot read; `details.loadcoach_code` and `details.loadcoach_details` carry LoadCoach's own failure verbatim, `details.http_status` the status it came with |
| `UNAUTHORIZED` | 401 | No usable bearer token where one is required |
| `FORBIDDEN` | 403 | The token's scopes do not contain the one this endpoint requires |
| `INTERNAL_ERROR` | 500 | Unhandled exception |

Full mapping table (LoadCoach code → PromptCadence behaviour → surfaced code) is spec §13; it is the
complete translation from every code LoadCoach can return, so no LoadCoach failure reaches a caller
as `INTERNAL_ERROR`. Behavioural notes:

* `PLAN_REJECTED` (once raisable) always lists every step's verdict and the policy or ceiling that
  rejected it.
* A denied egress evaluation is not an exception path: the turn ends with a structured refusal in
  the transcript, the `EgressDecision` is persisted, and the deviation policy decides whether the
  trajectory continues on a permitted tier or halts.
* A tool refusal (sandbox, allowlist, schema) is returned to the model as a structured `ToolResult`,
  never an exception — one refused call does not end a trajectory.
* Budget exhaustion mid-trajectory transitions to `awaiting_approval` (a ceiling raise is an
  approval) or halts per `on_exhausted`; exhaustion of the per-day ceiling may instead park the
  trajectory at `awaiting_window` until the next UTC day, for at most `window_wait_max_days`, then
  halts.
* A generic `StarletteHTTPException` (routing-level, not a domain error) is translated by HTTP
  status: `404 NOT_FOUND`, `405 METHOD_NOT_ALLOWED`, `413 PAYLOAD_TOO_LARGE`,
  `415 UNSUPPORTED_MEDIA_TYPE`, `421 MISDIRECTED_REQUEST` (a `Host` header outside the allowlist).

## 9. Authentication

Loopback with no tokens: open — the principal is `loopback`, holds every scope, and its grants are
recorded as `approver:loopback` (the record still says who). Once any token exists, or the bind is
not loopback, every scoped endpoint needs `Authorization: Bearer <token>` (`401` without one,
`403` without the scope). `GET /version` requires no scope at all.

Scopes are a **set, not a ladder** — unlike LoadCoach's cumulative reading, only `admin` contains
the others here:

| Scope | Grants |
|---|---|
| `read` | health, status, trajectories (read), explanations, plan, intents, turns, approvals (read), ledger (read), egress-decisions (read), tiers, tools, settings (read) |
| `write` | submit, cancel |
| `approve` | resolve approval requests — deliberately separate from `write`, so the identity that submits work cannot approve its own egress |
| `admin` | settings (change), tokens — contains `read`, `write` and `approve` |

A token carries any subset of `{read, write, approve, admin}`; `admin` alone grants everything else.
The operator console authenticates exactly as the API does and adds no session cookie
([ADR-0094](../../adr/0094-the-console-authenticates-as-the-api-does.md)): an open loopback install
browses and approves as `loopback`, and once a token exists, or the bind is not loopback, the
console answers `401` and names `promptcadence token create`.

Per-token rate limits and the failed-authentication brake follow LoadCoach's model
([Security Standards §14](../../standards/security-standards.md)): `[server] rate_limit_burst`
requests may arrive at once, then `rate_limit_per_minute` sustained, with `429 RATE_LIMITED` and a
`Retry-After` header at the boundary; only `/api/v1` is limited and `/version` is exempt.
`failed_auth_per_minute` braked per address, whatever credential it presents next.

## 10. Client guidance

1. `GET /api/v1/version` on first contact; verify the API major; cache with a TTL (`LoadCoachClient`
   does the same against LoadCoach — this is the same negotiation, one hop further out).
2. Prefer `/trajectories/{id}/stream` for interactive following and polling `GET /trajectories/{id}`
   for background work.
3. Treat PromptCadence being unreachable as a hard stop — it has no direct-provider fallback (spec
   §3): there is nowhere else for a governed call to go.
4. Read `cause` and `error_code` on a halted or failed trajectory; both are meant to be shown to an
   operator verbatim.
5. Poll `GET /approvals?status=pending` (or watch the stream) for work that needs a human; grant or
   deny promptly — a request that expires unresolved is a timeout, not a grant.
6. A remote-tier selection is marked as egress in the API response, the UI and the explanation; read
   `GET /egress-decisions` for the durable record of every approval, denial and violation.
7. Read `GET /ledger` before assuming a trajectory can spend more — the per-day and per-project
   ceilings bind independently of the trajectory's own, and the most restrictive wins.
