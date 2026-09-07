# Operations

## Where things live

| What | Where |
|---|---|
| Configuration | `$XDG_CONFIG_HOME/promptcadence/config.toml` (`promptcadence config path`) |
| Database, backups | `$XDG_DATA_HOME/promptcadence/promptcadence.sqlite3`, `…/backups/` |
| Workspaces | `<data>/workspaces/<trajectory_id>` — one per trajectory that called a tool |
| Artifacts | `<data>/artifacts/` — oversize tool output and materialized explanations, by digest |
| Logs | stdout, structured JSON off a TTY (`logging.format`) |

PostgreSQL is supported (`storage.database_url = "postgresql+psycopg://…"`); nothing else is. On
PostgreSQL `auto_migrate` defaults to false: run `promptcadence db upgrade` yourself.

## Day to day

```bash
promptcadence health                  # ok / degraded / unavailable, per component
promptcadence doctor                  # the same four components, with a remedy each
promptcadence tiers check             # every tier's task profile in LoadCoach; remote tiers' reasons
promptcadence trajectory list         # newest first; --state to filter
promptcadence approvals list          # what is waiting for a person, oldest first
promptcadence ledger show --scope day # today's spend against the per-day ceiling
promptcadence egress list --denied-only
promptcadence settings list           # the five runtime-changeable keys, and which layer decided
```

Those five keys can also be changed while the server runs, without editing a file or restarting:

```bash
promptcadence settings set execution.step_retries 3   # admin scope; --token, else $PROMPTCADENCE_API_TOKEN
promptcadence settings get execution.step_retries     # read scope
```

`settings set` prints the key's **effective** value after the write, which is not always what was
sent: a stored value is ignored while the same key is set in the environment (configuration
standards §7 — `defaults → file → database → env → CLI`), and the command says so rather than
reporting a success. Anything that decides exposure, egress, credentials, containment, retention
or spend is refused by name with `FORBIDDEN`; the budget ceilings are among them deliberately —
raising one is an approval with an approver on the record, never a form
(ADR-0100). `promptcadence config show`
answers the read-side question with the server stopped, marking what the settings table decides
`(database)`.

The console at `/` shows the same: the dashboard, trajectories and their timelines, the approvals
inbox, tiers, tools, ledger, egress and system. Counts on paged tables are capped at the page
size and say so.

## Health, and what degrades it

`GET /api/v1/health` reports `database`, `loadcoach`, `tiers` and `tools`. Only the database is
required. `loadcoach` degrades when LoadCoach is unreachable — never unavailable, because
PromptCadence needs LoadCoach for execution and not for serving. `tiers` degrades when a
configured tier's task profile is missing or disabled in LoadCoach, or when a remote tier cannot
serve — with the one reason, `loadcoach_has_no_remote_provider` or `unpriced`. `tools` degrades
when `[tools] enabled` names a tool that could not be registered, and reports which isolation
rung `run_command` has (container → bwrap → none, in which case the tool refuses).

## The worker

The worker is threads in the serving process over the `trajectories` table — no broker. Every
`lease_seconds / 3` a running trajectory's lease is renewed; every `lease_seconds` the worker reaps
expired leases, expires pending approvals past `request_timeout_hours`, releases trajectories the
next UTC day admits, and **sweeps retention** (below). `max_concurrent_trajectories` threads run
at once; `max_concurrent_steps` above 1 dispatches only across disjoint surfaces.

**Restart.** A restart recovers every lease the process held: a `planning` trajectory is
redrafted; an `executing` one is reconciled against LoadCoach's own job record — a finished job's
answer is recorded, an in-flight job is cancelled and the turn repeated under the same intent, an
unreconcilable one halts `recovered_after_crash`. Nothing is lost, duplicated or stuck, and no
LoadCoach job is orphaned (spec §20 #9). The recovery summary is logged and on the System page.

## Retention

A terminal trajectory keeps its words for `storage.content_retention_hours` (24) after it
finished: transcript text and requested tool calls, the plan document and every step
description, tool arguments, result summaries and refusal details, the task, and the workspace
directory. Then the sweep removes them and stamps `content_scrubbed_at`; every digest, model
identity, tier, usage figure, ceiling verdict, egress decision, deviation, approval and event
stays, and the explanation is re-materialized from the scrubbed rows, so the trajectory still
explains itself. An in-flight trajectory is never swept. `storage.retain_content = true` keeps
everything (config-only).

## Backups

```bash
promptcadence db backup                        # SQLite: a consistent copy under …/backups/, rotated
promptcadence db backup --output /mnt/nas/promptcadence-$(date +%F).sqlite3
promptcadence db restore <file> --yes          # overwrites the current database; no prompt
promptcadence db status                        # current revision, head, integrity
```

Every migration takes a backup first and restores it if the migration fails. Artifacts and
workspaces are files beside the database; back them up with it if the explanations' bodies matter
to you — the rows are authoritative and `promptcadence db rebuild-explanations --drop` rebuilds
every explanation from them.

A database ahead of the installed code refuses at startup with `SchemaAhead`, naming both
revisions and the backup directory; the downgrade path is: stop the application, restore that
backup, install the older version — proved end to end by
`tests/integration/test_downgrade_and_schema_ahead.py`, with the backup/restore round trip itself
proved on both dialects by `tests/integration/test_migrations.py`.

## Budgets

Three ceilings are active on a labelled trajectory: its own (`--budget`/`--tokens` or the
configured default), the per-day money ceiling every trajectory shares, and its project's. Local
work is unpriced and never counts against a money ceiling (a local model's cost is `UNSUPPORTED`,
never `$0.00`); the per-trajectory token ceiling is the universal brake. On exhaustion the
trajectory parks for a ceiling raise (`on_exhausted = "approval"`) or halts; the per-day ceiling
may park it until the next UTC day (`on_daily_exhausted = "window"`), for at most
`window_wait_max_days`.

## What to watch

* `tiers: degraded` in health: a task profile is missing in LoadCoach, or a remote tier cannot
  serve; the detail names which and why.
* `tools: degraded`: `run_command` has no isolation rung on this host and refuses; install
  podman/docker or bwrap, or drop the tool from `[tools] enabled`.
* A trajectory in `awaiting_approval` for a long time: `promptcadence approvals list` names the
  ask; it expires into a halt after `request_timeout_hours`, never into a grant.
* `awaiting_window`: the per-day ceiling is spent; it resumes at the next UTC day.
* `429 RATE_LIMITED` in a client's log: past the per-credential limit or the failed-authentication
  brake; `Retry-After` says when.
* `COMPACTION_FAILED`: a tier's context budget is smaller than the untouchable turns; raise
  `context_budget_tokens` or lower `protected_recent_turns`.
