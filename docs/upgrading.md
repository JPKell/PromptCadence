# Upgrading and downgrading

## Upgrading

```bash
pip install --upgrade promptcadence
promptcadence db status              # current revision, head, integrity
promptcadence serve                  # migrates on start (SQLite) — or:
promptcadence db upgrade
```

Every migration takes a backup first (`…/backups/`, rotated by `storage.backup_retention`). On
SQLite a failed migration restores that backup automatically and reports both outcomes; on
PostgreSQL migrations are not automatic (`auto_migrate` defaults to false there) and a failed one
is reported with the backup to restore. The migration run turns SQLite foreign keys **off** for
its own connection and back on after: altering a column on SQLite rebuilds the table, and a
parent rebuild with enforcement on deletes its children.

`promptcadence db status` after the upgrade shows the revision this build expects; health shows
the `database` component `ok`.

### Migration notes

| Version | Migrations | What they add |
|---|---|---|
| 1.1.0 | none | **No migration, no schema change, and no stored value changed.** The runtime settings this release adds live in the `settings` table, which has existed since `0001` and was unused until now; upgrading from 1.0.x is `pip install --upgrade` and a restart. |
| 1.0.0 | `0008`–`0011` | `plan_steps.attempt` (the per-step retry); `compactions`; `explanation_revisions` and a nullable `plans.raw_document`; `trajectories.content_scrubbed_at` (the retention sweep's stamp). Additive throughout; no existing value changes. |
| 0.9.0b0 | `0001`–`0007` | The beta's schema. |

Upgrading a `0.9.0b0` database is `0007 → 0011` in one `db upgrade`; the release handoff records
the proof (a `0.9.0b0` wheel's database upgraded by the `1.0.0` wheel in a clean venv).

### Behaviour changes at 1.1.0

* **`GET /settings` and `PUT /settings` are served**, and the console has a Settings page. Five
  keys change while the server runs — `storage.content_retention_hours`, `compaction.threshold`,
  `execution.step_retries`, `execution.max_turns_per_step`, `planning.corrective_retries` — and
  the running worker applies a change at its next lease reap. Nothing changes for an install that
  never calls them: with no stored row, every key is exactly what the file and the environment
  said.
* **Those five keys now sit between the file and the environment in precedence**
  (`defaults → file → database → env → CLI`). A key you pin in the environment still wins, and
  `GET /settings` reports the stored row as shadowed rather than dropping it.
* **Every other key is refused by name.** `PUT /settings` answers `403 FORBIDDEN` naming a
  security-relevant key — the whole of `[server]`, `[loadcoach]`, `[approval]`, `[budget]`,
  `[tools]`, `[tiers]` and `[policy]`, plus the database URL, `auto_migrate`, `retain_content`,
  the planning switches and `logging.include_content` — and `400 VALIDATION_ERROR` naming an
  unknown one. Raising a budget ceiling stays what it was: a `ceiling_raise` approval with an
  approver on the record (ADR-0100).
* **`promptcadence config show` marks database-sourced values `(database)`** and prints the stored
  value. With no database — absent, unmigrated, on another host — it prints exactly what it
  printed at 1.0.

### Behaviour changes at 1.0.0

* **Every `/api/v1` route except `/version` resolves a principal.** On an open loopback install
  nothing changes. Once a token exists, a client that previously reached `POST /trajectories`,
  the reads, health or the ledger anonymously now needs a bearer with the scope (`read`, `write`,
  `approve`). Client-mode CLI commands present `$PROMPTCADENCE_API_TOKEN` on every call.
* **The retention sweep runs.** A terminal trajectory loses its transcript text, plan document and
  step descriptions, tool arguments, task and workspace directory `storage.content_retention_hours`
  (24) after it finished. Set `retain_content = true` before upgrading if you rely on old text
  staying readable; the digests, decisions and events stay either way.
* **The two `[server]` limits are enforced**: `rate_limit_per_minute` (600, per credential) with
  the new `rate_limit_burst` (100) and `failed_auth_per_minute` (20), and `max_body_bytes`
  (1 MiB). A cross-origin JSON write is refused; scripts send no `Origin` and are unaffected.
* **Replayed tool-call arguments over 16 KiB** carry ToolYard's size-and-digest object back to
  the model instead of the arguments (ADR-0096). The call itself runs with them in full.
* **`turn_overrun` parks before `STEP_LIMIT_EXCEEDED` halts**, as lifecycle §5 always said; only
  the round-trip cap `max_turns_per_step` halts outright.
* **Remote tiers read the remote-provider fact from LoadCoach** (ADR-0098) and report one reason
  when they cannot serve — `loadcoach_has_no_remote_provider` or `unpriced` — in `tiers check`,
  `doctor`, the new `GET /tiers` and the console. LoadCoach 1.1.0 renders the flag on responses
  and not in `/models`, so on that version the fact reads absent and remote tiers refuse honestly.
* **The context-compaction target and threshold are one figure** (Phase 8): `threshold ×
  context_budget_tokens` is both the trigger and what the transcript is compacted back to.

### Compatibility

PromptCadence 1.0.1 is tested against LoadCoach `1.1.1` and needs LoadCoach `≥ 1.1` for the
declared finish reason on the wire, tool definitions on `/generate`, and the registration's
`is_remote` on responses. The requirement did not move at 1.0.1; the contract tests simply vendor
`1.1.1`'s `openapi.json` and `task_profiles.toml` rather than `1.1.0`'s. Suite packages:
`baseaicore >=0.4.1,<0.5`, `setspec >=0.5,<0.7`, `weightsdb >=0.2,<0.3`, `mirrorwall >=0.2,<0.3`,
`toolyard >=0.1.1,<0.2`, `cutctx >=0.1,<0.2`, `loadledger[sql] >=0.2,<0.3`,
`commissioner[sql] >=0.1,<0.2`.

The one floor that moved at 1.0.1 is `toolyard`, from `0.1` to `0.1.1`: the loop imports
`json_sanitize`, which ToolYard made public in `0.1.1` so that the `tool.call.started` event can
digest the same value the `tool_call_records` row does. Nothing else about the dependency changed,
and no other package moved.

## Downgrading

Downgrading the application without downgrading the database is **refused**, not attempted: a
database ahead of the code raises `SCHEMA_AHEAD` at startup and names both revisions and the
backup directory (packaging standards §6.1).

The supported path:

1. Stop the application.
2. Restore the automatic pre-migration backup:
   `promptcadence db restore <data>/backups/<file> --yes` — or, where the migration's
   `downgrade()` is lossless, downgrade with Alembic through `weightsdb`'s runner.
3. Install the older version and start it.

`0009`–`0011`'s downgrades drop `compactions`, `explanation_revisions` and the scrub stamp;
`explanation_revisions` is a cache and loses nothing, but `compactions` is the record that the
wire differed from the rows, so the backup path is the one to prefer.
