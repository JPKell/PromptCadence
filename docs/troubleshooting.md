# Troubleshooting

Start with `promptcadence doctor`: it reports the four components — `database`, `loadcoach`,
`tiers`, `tools` — each with what it found and what to do. Every API error is the standard envelope
— `code`, `message`, `details`, `request_id` — and the request ID is in the log line that goes
with it. Every halt names its cause verbatim: `promptcadence trajectory show <id>`.

## Startup

| Symptom | Code | What it means and what to do |
|---|---|---|
| Refuses to start naming a field | `CONFIGURATION_ERROR` | `promptcadence config validate` shows the problem; the field is named. A remote tier without `max_data_classification` or `pricing_file`, a project budget binding neither ceiling, a read root overlapping the workspace root, an unknown compaction policy — each is refused here, never discovered mid-trajectory. |
| Refuses to start on a non-loopback host | `INSECURE_BINDING` | Set `server.allowed_hosts`, create a token (`promptcadence token create`), and set `allow_lan_exposure` only for `0.0.0.0`. `approval.mode = "manual"` additionally needs an `approve`-scoped token. See security.md. |
| "database is behind head" | `MIGRATION_REQUIRED` | `promptcadence db upgrade`. Automatic on SQLite unless `storage.auto_migrate = false`; never automatic on PostgreSQL. |
| "database was written by a newer version" | `SCHEMA_AHEAD` | Install that version, or restore the pre-migration backup and stay. See upgrading.md. |
| Cannot open the database | `DATABASE_UNAVAILABLE` | The directory, the URL, or the server. `promptcadence db status`. |
| A tier's `pricing_file` is unreadable | `CONFIGURATION_ERROR` | Read once at startup, deliberately: a price list that fails halfway through a trajectory would leave real spend nobody can cost. |

## `doctor`'s four components

| Component | Degraded when | What to do |
|---|---|---|
| `database` | — (unavailable when it cannot be opened) | `promptcadence db status`; the URL and the directory. |
| `loadcoach` | LoadCoach is unreachable, or answers non-JSON, or reports itself degraded | Start LoadCoach; check `[loadcoach] base_url`; PromptCadence keeps serving meanwhile and a submitted trajectory fails `LOADCOACH_UNAVAILABLE` at its first turn. |
| `tiers` | A configured tier's task profile is missing or disabled in LoadCoach; `tools.plan` is missing; a remote tier cannot serve | `promptcadence tiers check` names the profile. A remote tier's reason is `loadcoach_has_no_remote_provider` (register one in LoadCoach — and note LoadCoach 1.1.0 renders the flag on responses, not in `/models`, so it reads absent there) or `unpriced` (add an ADR-0072 record). See tiers.md. |
| `tools` | `[tools] enabled` names a tool that could not be registered; `run_command` has no isolation rung | `promptcadence tools list` shows the withheld cause and the rung; install podman/docker or bwrap, or drop the tool. |

## Submission and approval

| Symptom | Code | What it means and what to do |
|---|---|---|
| 400 on submit | `VALIDATION_ERROR`, `CLASSIFICATION_INVALID` | The body names the field; classifications are `public`, `internal`, `confidential`. |
| 422 on submit | `PROJECT_UNKNOWN`, `TOOL_NOT_FOUND`, `TIER_NOT_CONFIGURED` | `project` must name a `[budget.projects.<name>]`; `tools` must be a subset of the registry (`tools list`); `tier` a configured tier. |
| Halted after planning | `PLAN_DRAFT_FAILED` | The planner returned nothing valid in `corrective_retries + 1` attempts; every attempt is on `GET /trajectories/{id}/plan` with its issues. On gpt-oss:20b an empty document is `finish_reason = length` against `tools.plan`'s output budget (G2 measured it); shorten the task or raise the profile's budget in LoadCoach. |
| Halted at approval | `PLAN_REJECTED` | Every step's verdict and the policy or ceiling that rejected it is on the plan record — never "the plan was refused" without numbers. |
| Parked | `awaiting_approval` | `promptcadence approvals list` shows the kind: a held `plan`, a `gated_step`, the bypass path's `bypass_gate`, a scoped `reapproval` after a deviation, or a `ceiling_raise`. `approve`/`deny`; a request expires into a halt after `request_timeout_hours`. |
| 409 on approve/deny | `APPROVAL_INVALID_STATE` | The request is already resolved; a second grant of a granted request answers 200 with `already_resolved: true`. |

## Execution

| Symptom | Code | What it means and what to do |
|---|---|---|
| LoadCoach down mid-turn | `LOADCOACH_UNAVAILABLE` | The trajectory fails at T13 after cancelling any job the request started; no silent retry. |
| A LoadCoach error | `LOADCOACH_ERROR` | The original code is in `details`. A service failure (`ALL_CANDIDATES_FAILED`, `PROVIDER_TIMEOUT`, `QUEUE_FULL`, `RATE_LIMITED`, the client's timeout) is repeated under the same intent up to `step_retries`, then halts naming every attempt; a deterministic refusal is never repeated. |
| No model can serve the tier | `TIER_UNAVAILABLE` | Falls to the intent's next tier, then a `tier_escalation` re-approval, then this halt naming the exhausted order. `reason = task_profile_not_found` means the profile is missing in LoadCoach. |
| A remote tier with no price | `UNPRICED_EGRESS_REFUSED` | Before any call. Add an ADR-0072 record claiming the current instant (tiers.md). |
| Confidential data, remote tier | `EGRESS_DENIED` | Before any call; the refusal is a queryable `EgressDecision` (`egress list --denied-only`). Declare the trajectory `internal`/`public` if that is true, or use a local tier. |
| The step ran out of turns | `STEP_LIMIT_EXCEEDED` | `max_turns_per_step` tool round trips spent with no declared finish. Reaching the intent's `max_turns` is different: that parks as a `turn_overrun` re-approval that may extend it. |
| A tool call refused | `TOOL_REFUSED` (in the record) | The record's `reason`: `unknown_tool`, `not_approved`, `args_invalid`, containment, `isolation_unavailable`, `egress_not_permitted`. A refusal is a tool turn back to the model, never a halt. |
| Halted on a deviation | `DEVIATION_HALTED` | A `tier_violation` (a remote provider answered a local tier), or more than three deviations on one step. The deviation rows and the egress `VIOLATION` decision are on the record. |
| Compaction cannot fit | `COMPACTION_FAILED` | The untouchable turns alone exceed the tier's context budget, or no local tier admits the step's classification to summarize on. Raise the budget or lower `protected_recent_turns`. |
| Budget crossed | `BUDGET_EXCEEDED`, `TOKEN_BUDGET_EXCEEDED` | The ledger shows every debit and the balance that crossed; `on_exhausted` decides park or halt. |

## Access

| Symptom | Code | What it means and what to do |
|---|---|---|
| 401 | `UNAUTHORIZED` | Tokens exist (or the bind is not loopback) and no usable bearer was presented. Client-mode commands read `$PROMPTCADENCE_API_TOKEN`. |
| 403 | `FORBIDDEN` | The token lacks the scope; `details.required` and `details.held`. Remember `approve` is not `write`. |
| 403 on a form or a JSON write | `CSRF_FAILED` | The double-submit token is missing or wrong, or the JSON write came from another origin. |
| 413 | `PAYLOAD_TOO_LARGE` | Over `server.max_body_bytes`. |
| 421 | `MISDIRECTED_REQUEST` | The `Host` header is not in the allowlist; DNS rebinding defence. |
| 429 with `Retry-After` | `RATE_LIMITED` | Past the per-credential limit or the failed-authentication brake. Wait the named seconds. |
| The console answers 401 | `UNAUTHORIZED` | A token exists or the bind is not loopback; the console is loopback-first by decision (ADR-0094). |
| 403 from `PUT /settings` | `FORBIDDEN` | Either the key is security-relevant and config-only (`details.key` names it — change it in `config.toml` or the environment and restart), or the token lacks `admin`. |

## A setting was saved and nothing changed

Two causes, in this order.

* **The environment pins that key.** Runtime settings sit between the file and the environment
  (configuration standards §7), so `PROMPTCADENCE_EXECUTION__STEP_RETRIES` beats a stored row.
  `GET /settings` says so per key — `shadowed_by` names the variable, and `stored` shows the row
  that is doing nothing — and the Settings page prints the same beneath the field. Unset the
  variable (`promptcadence serve` passes its own flags as environment variables, so a flag counts)
  and the stored value takes effect at the next lease reap.
* **The reap has not come round.** A change is applied by the running worker at its next
  lease-reap cadence, which is `execution.lease_seconds` (60 by default), not on the next turn.
  `promptcadence config show` reads the database directly and marks the value `(database)`
  immediately, whatever the worker has done so far.

## Storage

`STORAGE_FULL` (507) — free disk beside the database. `STORAGE_BUSY` (503) — SQLite lock
contention beyond the busy timeout; another process is holding a write. Both are reported, never
retried silently.
