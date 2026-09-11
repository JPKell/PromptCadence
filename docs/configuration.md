# Configuration reference

**Generated** from `promptcadence.config.Settings` by `promptcadence config reference`; do not
edit by hand — `tests/unit/test_config_reference.py` fails when this file differs from the model.

Precedence, field by field (configuration standards §1): built-in defaults, then `config.toml`
(`promptcadence config path` prints where), then `PROMPTCADENCE_*` environment variables, then CLI
flags. Sections and fields are joined with a double underscore in the environment: `[server] port`
is `PROMPTCADENCE_SERVER__PORT`. Lists are comma-separated in the environment. A keyed table —
`[tiers.<name>]`, `[budget.projects.<name>]` — puts the key between the section and the field:
`PROMPTCADENCE_TIERS__LOCAL_FAST__CONTEXT_BUDGET_TOKENS`. Setting any `TIERS__<name>__*` key
replaces the shipped default tier map rather than extending it.

**Runtime-changeable** is `yes` for the five keys `PUT /settings` and the console's Settings page
can change while the server runs; the running worker applies them at its next lease reap. Those
five sit between the file and the environment in precedence (configuration standards §7):
`defaults → file → database → env → CLI`, so a key pinned in the environment keeps its value and
the stored row is reported as shadowed. `promptcadence config show` marks a value the database
decides `(database)`. Every other key is `no` — a file or environment edit and a restart — and
those that decide exposure, egress, credentials, containment, retention or spend are refused by
name with `FORBIDDEN` if they are sent to the API at all (spec §14, ADR-0100); read
`docs/security.md` before changing one on a non-loopback bind. The **Range** column is the field's
own: the API and the page bound the five runtime-changeable keys more narrowly than the file does,
and `GET /settings` reports each one's minimum and maximum.


## `[server]`

``[server]`` — bind address and HTTP-level limits.

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `server.host` | `PROMPTCADENCE_SERVER__HOST` | `str` | `'127.0.0.1'` | — | no | Non-loopback exposes the service; requires allowed_hosts and a token. | `'127.0.0.1'` | Interface to bind. Loopback by default; anything else requires allowed_hosts and at least one active API token (ADR-0026). |
| `server.port` | `PROMPTCADENCE_SERVER__PORT` | `int` | `8768` | ≥ 1, ≤ 65535 | no | Part of the exposure decision. | `8768` |  |
| `server.allow_lan_exposure` | `PROMPTCADENCE_SERVER__ALLOW_LAN_EXPOSURE` | `bool` | `False` | — | no | Acknowledges binding every interface. | — | Acknowledges a deliberate bind to every interface (0.0.0.0). Without it such a bind refuses to start. |
| `server.allowed_hosts` | `PROMPTCADENCE_SERVER__ALLOWED_HOSTS` | `tuple[str, Ellipsis]` | `()` | — | no | DNS-rebinding defence on a non-loopback bind (ADR-0026 §1). | `['promptcadence.local']` | Host header values accepted on a non-loopback bind, against DNS rebinding. Comma-separated in the environment. |
| `server.rate_limit_per_minute` | `PROMPTCADENCE_SERVER__RATE_LIMIT_PER_MINUTE` | `int` | `600` | ≥ 0 | no | Keeps one credential from starving others. | `600` | Requests per minute one credential may make to /api/v1, sustained (spec §14). A token bucket: rate_limit_burst may arrive at once, then this rate. 0 disables. At the limit a caller gets 429 RATE_LIMITED with Retry-After, never a dropped request. |
| `server.rate_limit_burst` | `PROMPTCADENCE_SERVER__RATE_LIMIT_BURST` | `int` | `100` | ≥ 1 | no | Keeps one credential from starving others. | `100` | How many requests one credential may make at once before the rate applies. |
| `server.failed_auth_per_minute` | `PROMPTCADENCE_SERVER__FAILED_AUTH_PER_MINUTE` | `int` | `20` | ≥ 0 | no | Brakes credential guessing per address. | `20` | Failed authentications one address may make per minute before it is refused with 429 for the rest of the minute (ADR-0014 §6). 0 disables. |
| `server.max_body_bytes` | `PROMPTCADENCE_SERVER__MAX_BODY_BYTES` | `int` | `1048576` | ≥ 1024 | no | Bounds what a caller can make the server buffer. | `1048576` | The largest request body accepted, refused with 413 before buffering (Security Standards §14). A trajectory submission is a few kilobytes; nothing here parses a document. |

## `[storage]`

``[storage]`` — database location and transcript retention (mirrors LoadCoach).

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `storage.database_url` | `PROMPTCADENCE_STORAGE__DATABASE_URL` | `str \| None` | `None` | — | no | Where every trajectory, transcript and token digest lives. | — | SQLAlchemy URL. Unset resolves to a SQLite file under the XDG data directory. |
| `storage.auto_migrate` | `PROMPTCADENCE_STORAGE__AUTO_MIGRATE` | `bool` | `True` | — | no | — | — | Migrate on startup. Defaults true on SQLite; a PostgreSQL URL turns it off, because a failed migration there cannot be rolled back automatically (database standards §5.1). |
| `storage.content_retention_hours` | `PROMPTCADENCE_STORAGE__CONTENT_RETENTION_HOURS` | `int` | `24` | ≥ 0 | yes | How long finished text and workspaces are kept. | — | How long transcript text (turn and tool-call content) is kept after a trajectory finishes; records, hashes, usage and decisions are kept forever regardless. |
| `storage.retain_content` | `PROMPTCADENCE_STORAGE__RETAIN_CONTENT` | `bool` | `False` | — | no | Keeps transcript text and workspaces for ever (spec §14). | — | Keep transcript text, plan documents, tool arguments and workspaces for ever instead of sweeping them content_retention_hours after a trajectory finishes (spec §14). Config-only, mirroring LoadCoach's. |

## `[loadcoach]`

``[loadcoach]`` — the only path to a model (ADR-0045). Never required at startup.

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `loadcoach.base_url` | `PROMPTCADENCE_LOADCOACH__BASE_URL` | `str` | `'http://127.0.0.1:8766'` | — | no | Where every prompt is sent (ADR-0045). | `'http://127.0.0.1:8766'` |  |
| `loadcoach.api_key_env` | `PROMPTCADENCE_LOADCOACH__API_KEY_ENV` | `str` | `''` | — | no | A credential; resolved through the secret chain, never logged. | — | Name of the environment variable holding the token, or empty (ADR-0026). |
| `loadcoach.api_key_file` | `PROMPTCADENCE_LOADCOACH__API_KEY_FILE` | `str` | `''` | — | no | A credential; resolved through the secret chain, never logged. | — | Path to a file holding the token, or empty. Mutually exclusive with above. |
| `loadcoach.timeout_seconds` | `PROMPTCADENCE_LOADCOACH__TIMEOUT_SECONDS` | `float` | `600.0` | > 0 | no | — | `600.0` |  |

## `[planning]`

``[planning]`` — the bypass switch. Governance is never bypassed, only planning is.

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `planning.enabled` | `PROMPTCADENCE_PLANNING__ENABLED` | `bool` | `True` | — | no | — | — |  |
| `planning.allow_request_override` | `PROMPTCADENCE_PLANNING__ALLOW_REQUEST_OVERRIDE` | `bool` | `True` | — | no | — | — | Permit a per-request bypass_planning override. |
| `planning.reapproval_scope` | `PROMPTCADENCE_PLANNING__REAPPROVAL_SCOPE` | `'on_tier_or_classification_change' \| 'any_deviation'` | `'on_tier_or_classification_change'` | — | no | — | — |  |
| `planning.max_plan_steps` | `PROMPTCADENCE_PLANNING__MAX_PLAN_STEPS` | `int` | `20` | ≥ 1 | no | — | — |  |
| `planning.corrective_retries` | `PROMPTCADENCE_PLANNING__CORRECTIVE_RETRIES` | `int` | `2` | ≥ 0 | yes | — | — | How many corrective retries the planner may spend after an invalid draft (lifecycle §4.1's bounded corrective retry). 0 means one attempt and no retry; the draft fails with PLAN_DRAFT_FAILED once the budget is spent. |

## `[approval]`

``[approval]`` — who authorizes the minting of an ``ExecutionIntent`` (ADR-0049).

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `approval.mode` | `PROMPTCADENCE_APPROVAL__MODE` | `'auto' \| 'hybrid' \| 'manual'` | `'auto'` | — | no | Who authorizes execution; manual needs an approve-scoped token. | — |  |
| `approval.gate_egress_at` | `PROMPTCADENCE_APPROVAL__GATE_EGRESS_AT` | `DataClassification` | `<DataClassification.INTERNAL: 'internal'>` | — | no | The classification at or above which egress needs a person. | — |  |
| `approval.gate_step_cost` | `PROMPTCADENCE_APPROVAL__GATE_STEP_COST` | `table` | — | — | no | — | — |  |
| `approval.request_timeout_hours` | `PROMPTCADENCE_APPROVAL__REQUEST_TIMEOUT_HOURS` | `float` | `24.0` | > 0 | no | — | — |  |

## `[execution]`

``[execution]`` — concurrency and loop bounds.

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `execution.max_concurrent_trajectories` | `PROMPTCADENCE_EXECUTION__MAX_CONCURRENT_TRAJECTORIES` | `int` | `1` | ≥ 1 | no | — | — |  |
| `execution.max_concurrent_steps` | `PROMPTCADENCE_EXECUTION__MAX_CONCURRENT_STEPS` | `int` | `1` | ≥ 1 | no | — | — |  |
| `execution.max_concurrent_remote_steps` | `PROMPTCADENCE_EXECUTION__MAX_CONCURRENT_REMOTE_STEPS` | `int` | `2` | ≥ 1 | no | — | — |  |
| `execution.max_turns_per_step` | `PROMPTCADENCE_EXECUTION__MAX_TURNS_PER_STEP` | `int` | `8` | ≥ 1 | yes | — | — | How many model round trips one step may take before it halts with no declared finish (STEP_LIMIT_EXCEEDED). Retries share this envelope, so step_retries binds inside it; a step that parks on turn_overrun has not spent it. |
| `execution.step_retries` | `PROMPTCADENCE_EXECUTION__STEP_RETRIES` | `int` | `1` | ≥ 0 | yes | — | — | How many times a step's failed turn is repeated under the same ExecutionIntent revision (ADR-0076). 0 means one attempt and no repeat; the trajectory then halts naming the last cause and every attempt. Only a LoadCoach service failure that could plausibly answer differently is repeated — a governance outcome never is. There is no backoff, and attempts share the envelope with turns, so max_turns_per_step binds too. |
| `execution.max_steps` | `PROMPTCADENCE_EXECUTION__MAX_STEPS` | `int` | `20` | ≥ 1 | no | — | — |  |
| `execution.lease_seconds` | `PROMPTCADENCE_EXECUTION__LEASE_SECONDS` | `int` | `60` | ≥ 1 | no | — | — |  |

## `[budget]`

``[budget]`` — the two ceilings, because one alone cannot bind (ADR-0047 §3).

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `budget.default_money_ceiling` | `PROMPTCADENCE_BUDGET__DEFAULT_MONEY_CEILING` | `table` | — | — | no | — | — |  |
| `budget.default_token_ceiling` | `PROMPTCADENCE_BUDGET__DEFAULT_TOKEN_CEILING` | `int` | `2000000` | ≥ 1 | no | — | — |  |
| `budget.daily_money_ceiling` | `PROMPTCADENCE_BUDGET__DAILY_MONEY_CEILING` | `table` | — | — | no | — | — |  |
| `budget.estimate_min_samples` | `PROMPTCADENCE_BUDGET__ESTIMATE_MIN_SAMPLES` | `int` | `20` | ≥ 1 | no | — | — |  |
| `budget.partial_pricing` | `PROMPTCADENCE_BUDGET__PARTIAL_PRICING` | `'floor' \| 'strict'` | `'floor'` | — | no | — | — |  |
| `budget.on_exhausted` | `PROMPTCADENCE_BUDGET__ON_EXHAUSTED` | `'approval' \| 'halt'` | `'approval'` | — | no | — | — |  |
| `budget.on_daily_exhausted` | `PROMPTCADENCE_BUDGET__ON_DAILY_EXHAUSTED` | `'window' \| 'approval' \| 'halt'` | `'window'` | — | no | — | — |  |
| `budget.window_wait_max_days` | `PROMPTCADENCE_BUDGET__WINDOW_WAIT_MAX_DAYS` | `int` | `3` | ≥ 1 | no | — | — |  |
| `budget.projects` | `PROMPTCADENCE_BUDGET__PROJECTS` | `dict[str, table]` | — | — | no | — | — |  |

## `[budget.projects.<name>]`

One ``[budget.projects.<name>]`` entry: a labelled ceiling binding a project's work.

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `budget.projects.<name>.money_ceiling` | `PROMPTCADENCE_BUDGET__PROJECTS__<NAME>__MONEY_CEILING` | `table \| None` | `None` | — | no | — | — |  |
| `budget.projects.<name>.token_ceiling` | `PROMPTCADENCE_BUDGET__PROJECTS__<NAME>__TOKEN_CEILING` | `int \| None` | `None` | ≥ 1 | no | — | — |  |

## `[tools]`

``[tools]`` — the registry the loop draws from, and where its side effects land.

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `tools.enabled` | `PROMPTCADENCE_TOOLS__ENABLED` | `tuple[str, Ellipsis]` | `('read_file', 'list_dir', 'write_file', 'run_command', 'http_fetch')` | — | no | Which tools a model can be offered at all. | — |  |
| `tools.workspace_root` | `PROMPTCADENCE_TOOLS__WORKSPACE_ROOT` | `str` | `''` | — | no | Where model-directed writes land; containment root. | — | Default: <data>/workspaces, per-trajectory subdirectory. |
| `tools.artifact_root` | `PROMPTCADENCE_TOOLS__ARTIFACT_ROOT` | `str` | `''` | — | no | — | — | Where an oversize tool output is filed, keyed by the digest of the whole output. Default: <data>/artifacts. |
| `tools.read_roots` | `PROMPTCADENCE_TOOLS__READ_ROOTS` | `tuple[str, Ellipsis]` | `()` | — | no | Extra read-only roots a model may read from. | — |  |
| `tools.fetch_allowed_hosts` | `PROMPTCADENCE_TOOLS__FETCH_ALLOWED_HOSTS` | `tuple[str, Ellipsis]` | `()` | — | no | The outbound fetch allowlist (ADR-0026 §3). | — |  |
| `tools.fetch_max_data_classification` | `PROMPTCADENCE_TOOLS__FETCH_MAX_DATA_CLASSIFICATION` | `DataClassification \| None` | `None` | — | no | The ceiling http_fetch's egress is governed by. | — | The ceiling http_fetch's non-loopback egress is governed by. Absent by default, which denies every non-loopback fetch with `no_ceiling_declared` (ADR-0046: an undeclared ceiling is never assumed public). Loopback needs none - it is not egress. |
| `tools.redact_args` | `PROMPTCADENCE_TOOLS__REDACT_ARGS` | `tuple[str, Ellipsis]` | `()` | — | no | Tool names whose arguments are stored as a digest only. | — |  |
| `tools.container_image` | `PROMPTCADENCE_TOOLS__CONTAINER_IMAGE` | `str` | `'python:3.12-slim'` | — | no | The image run_command's container rung runs in. | — | The image run_command's container rung uses. Probed and run with --pull=never, so it must already be present locally; `doctor` shows which rung the ladder landed on. |
| `tools.max_result_chars` | `PROMPTCADENCE_TOOLS__MAX_RESULT_CHARS` | `int` | `8192` | ≥ 256 | no | — | — | How much of a tool result the model is shown before a labelled truncation. Separate from what is stored: the whole output is kept as an artifact under its digest. |
| `tools.timeout_seconds` | `PROMPTCADENCE_TOOLS__TIMEOUT_SECONDS` | `float` | `30.0` | > 0 | no | — | — | The per-call limit; there is no way to express no limit. |

## `[compaction]`

``[compaction]`` — the CutCtx trigger (lifecycle §7), live from Phase 8.

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `compaction.threshold` | `PROMPTCADENCE_COMPACTION__THRESHOLD` | `float` | `0.8` | > 0, ≤ 1 | yes | — | — | Compact when the transcript estimate exceeds this fraction of the tier's context_budget_tokens. The budget compacted *to* is the whole tier budget, not this fraction of it: compacting to the threshold would fire the trigger again next turn. |
| `compaction.policy_chain` | `PROMPTCADENCE_COMPACTION__POLICY_CHAIN` | `tuple[str, Ellipsis]` | `('observation_masking', 'summarizing', 'drop_oldest')` | — | no | — | — | CutCtx policies, in the order they run, stopping as soon as the budget fits. The default order is an argument about cost: masking is free, summarizing costs a model call but keeps the substance, dropping keeps nothing. |
| `compaction.protected_recent_turns` | `PROMPTCADENCE_COMPACTION__PROTECTED_RECENT_TURNS` | `int` | `4` | ≥ 0 | no | — | — | How many turns at the end of the transcript no policy may touch. The framing block - the task and the step description - is pinned separately and is never in this count. |

## `[tiers.<name>]`

One ``[tiers.<name>]`` entry: configuration over exactly one LoadCoach task profile.

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `tiers.<name>.task_profile` | `PROMPTCADENCE_TIERS__<NAME>__TASK_PROFILE` | `str` | `''` | — | no | — | — |  |
| `tiers.<name>.remote` | `PROMPTCADENCE_TIERS__<NAME>__REMOTE` | `bool` | `False` | — | no | Declares a tier as egress; needs a ceiling and a price list. | — | The egress class. |
| `tiers.<name>.max_data_classification` | `PROMPTCADENCE_TIERS__<NAME>__MAX_DATA_CLASSIFICATION` | `DataClassification \| None` | `None` | — | no | The highest classification the tier may see. | — | Required when remote; never meaningful when local. |
| `tiers.<name>.context_budget_tokens` | `PROMPTCADENCE_TIERS__<NAME>__CONTEXT_BUDGET_TOKENS` | `int` | `16384` | ≥ 1 | no | — | — |  |
| `tiers.<name>.pricing_file` | `PROMPTCADENCE_TIERS__<NAME>__PRICING_FILE` | `str` | `''` | — | no | Unpriced egress is refused, not free (ADR-0030). | — | ModelPricing records; required when remote (ADR-0047 §3). |
| `tiers.<name>.default_step_input_tokens` | `PROMPTCADENCE_TIERS__<NAME>__DEFAULT_STEP_INPUT_TOKENS` | `int` | `4096` | ≥ 1 | no | — | — |  |
| `tiers.<name>.default_step_output_tokens` | `PROMPTCADENCE_TIERS__<NAME>__DEFAULT_STEP_OUTPUT_TOKENS` | `int` | `1024` | ≥ 1 | no | — | — |  |

## `[policy]`

``[policy]`` — where an unplanned or bypass turn starts, and how it escalates.

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `policy.default_tier` | `PROMPTCADENCE_POLICY__DEFAULT_TIER` | `str` | `'local_fast'` | — | no | — | — |  |
| `policy.escalation_order` | `PROMPTCADENCE_POLICY__ESCALATION_ORDER` | `tuple[str, Ellipsis]` | `('local_fast', 'local_large')` | — | no | — | — |  |

## `[logging]`

Structured-logging behaviour.

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `logging.level` | `PROMPTCADENCE_LOGGING__LEVEL` | `'DEBUG' \| 'INFO' \| 'WARNING' \| 'ERROR' \| 'CRITICAL'` | `'INFO'` | — | no | — | — |  |
| `logging.format` | `PROMPTCADENCE_LOGGING__FORMAT` | `'text' \| 'json' \| 'auto'` | `'auto'` | — | no | — | — |  |
| `logging.include_content` | `PROMPTCADENCE_LOGGING__INCLUDE_CONTENT` | `bool` | `False` | — | no | Logs transcript text at DEBUG when true (config-only). | — | Log full prompts and responses. Off by default: only hashes are logged. |

## `[console]`

Where WeightRoomGym is, when one fronts this application (row WM2).

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `console.url` | `PROMPTCADENCE_CONSOLE__URL` | `str` | `''` | — | no | — | `'https://jordan-main.local:8769'` | WeightRoomGym's base URL (https://<host>:8769); empty renders no application tab strip. |
