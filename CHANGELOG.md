# Changelog

All notable changes to `promptcadence` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per packaging and release standards §3.

## [Unreleased]

### Fixed
- **`test_the_running_worker_applies_a_write_within_one_reap_cadence` no longer flakes under
  machine load.** It polls a real background thread against a wall-clock deadline once the fake
  clock is advanced past the reap instant; the only real-time variable left is OS scheduling
  latency for the worker's `poll_interval_seconds` wake-up, not application logic. Widened from 10s
  to 30s with a `# ponytail:` comment naming the ceiling, after it failed once while a parallel
  LoadCoach coverage run was saturating the machine. Ran clean 10/10 in isolation both before and
  after; nothing about what the test proves changed.

## [1.2.0] — 2026-09-07

The CLI half of the settings surface. Row I5 built the registry, the API and the console page and
deliberately left the verb out; a setting a script or an ssh session needs to change should not
require hand-writing `curl`. No new runtime-changeable key, no endpoint, no schema change — the
five keys are as [ADR-0100](../docs/adr/0100-promptcadences-runtime-changeable-set.md) left them.

### Added

- **`promptcadence settings list | get <key> | set <key> <value>`** (spec §7.2). A **client**
  command in the shape of `approve` and `deny`: it talks to a running server over HTTP, so the
  scopes the API enforces are the scopes the CLI enforces — `read` to look, `admin` to change —
  and `--token`, else `$PROMPTCADENCE_API_TOKEN`, is the identity. `--json` on all three.
  `promptcadence config show` remains the read-side answer for a *stopped* install; this verb does
  not fall back to it.
- `set` prints the key's **effective** value after the write and names the variable when a stored
  row is shadowed by the environment (configuration standards §7), so a value that cannot take
  effect never reads as a success. A refusal prints the API's code and message — `FORBIDDEN`
  naming a security-relevant key (exit 1), `VALIDATION_ERROR` naming an unknown one and listing
  the changeable set from the envelope's own `details` (exit 2) — never a traceback; an
  unreachable server is exit 4. The registry is not restated in the CLI: a key added to
  `services/settings.py` needs no change there.

## [1.1.0] — 2026-09-06

The runtime settings spec §7.1 has listed since the specification was written. Five bounded tuning
numbers can now change while the server runs; everything that decides exposure, egress,
credentials, containment, retention or spend is refused by name. **No migration and no schema
change** — the `settings` table has existed since `0001` and was unused until now.

### Added
- **`GET /settings` and `PUT /settings`** (spec §7.1). `GET` is `read`-scoped and answers with, per
  key, the effective value, its type, bounds and description, the configured value, the stored row
  or `null`, which of the two is in force, and what shadows the row when the environment pins the
  key — a stored value that does nothing is visible as such rather than applied or dropped in
  silence. It also lists the config-only keys, named as configured. `PUT` is `admin`-scoped and
  answers with the same document.
- **A Settings page in the console**, with the registry as a form for `admin`, the effective
  values as a table for `read`, and the config-only keys listed in both cases — the page says what
  cannot be changed there as much as what can. The form posts through MirrorWall's double-submit
  token (ADR-0094).
- **The runtime-changeable registry**, one module the API, the page, the CLI and the generated
  `docs/configuration.md` all read: `storage.content_retention_hours`, `compaction.threshold`,
  `execution.step_retries`, `execution.max_turns_per_step`, `planning.corrective_retries`. Each is
  re-read by the running process — membership is tested by re-reading, not by plausibility — and
  the running worker applies a change at its next lease reap (ADR-0100).
- **`promptcadence config show` marks database-sourced values `(database)`** and prints the stored
  value, per configuration standards §7. With no readable database it prints exactly what it
  printed at 1.0.

### Changed
- **The five runtime-changeable keys sit between the file and the environment in precedence**
  (`defaults → file → database → env → CLI`), which is configuration standards §7 rather than
  LoadCoach's implementation: a key pinned in the environment — including by a `promptcadence
  serve` flag, which the CLI passes as one — keeps its value, and the stored row is reported as
  shadowed. No other key's precedence moved.
- **Spec §14's `admin` scope reads "settings changes, tokens".** Reading the effective settings is
  `read`: seeing what the process is running on is not the privileged half.
- The **Runtime-changeable** column of `docs/configuration.md` is rendered from the registry
  instead of the literal `no` every row carried.

### Fixed
- **The trajectory-explanation golden no longer pins a wall-clock measurement.** `duration_ms` on
  a tool call is `int(round(elapsed))` over a real monotonic source, so it is `0` on a machine that
  completes the call in under a millisecond and `1` on one that does not. The golden's mask turns
  timings into `<ms>` by *type*, which catches the float siblings (`loadcoach_ms`, `overhead_ms`)
  and missed this one because it is an `int` — so the document's shape check was also a benchmark
  of whatever ran it. It failed for the first time on CI's Python 3.12 job at 1.0.1, where the
  runner was simply slower; the version was incidental. `duration_ms` is now masked by name and the
  golden regenerated, and the file is otherwise byte-identical. Test-only; no shipped behaviour
  changes, and the six masked values are the only difference.

## [1.0.1] — 2026-09-06

A maintenance release: the two vendored LoadCoach snapshots move to LoadCoach 1.1.1, and I2's two
deferred findings are settled — one by naming the behaviour and making it visible, one by fixing a
digest that matched nothing.

### Fixed
- **The test suite no longer leaks a closed log stream between tests.** `configure_logging`
  installs a `StreamHandler` bound to the `sys.stderr` that exists when it is called, which is
  what the standard library does too. Under Click's `CliRunner` that stream is a temporary buffer,
  and the buffer closes when the invocation ends — but the handler stayed on the process-global
  root logger. The next record emitted from any thread (an application started by another test's
  `TestClient` runs its migrations on a portal thread, and Alembic logs while it does) then wrote
  to a closed file, and `logging` reported *that* failure to whatever `sys.stderr` was current —
  the next CLI invocation's captured output. A command that printed a JSON document was no longer
  the only thing in it, so `promptcadence trajectory explain --json` failed to parse under roughly
  one full-suite ordering in five. An autouse fixture now saves and restores the root logger's
  handlers, and the previously failing seed plus ten consecutive full runs are green. Test-only;
  no shipped behaviour changes.
- **`tool.call.started` digests the arguments its `tool_call_records` row digests.** The event
  digested the *canonical JSON text* of a call's arguments while ToolYard digests the *sanitized
  value* (`executor._args_digest`), so for every call whose arguments parsed the two `args_sha256`
  values differed and the pair could not be matched — which is the field's entire purpose, stated
  on `domain.tools.ToolCallStarted`: *"so an event and its row can be matched without either
  holding plaintext"*. The event now takes ToolYard's own `json_sanitize`, so the two agree by
  construction rather than by transcription, and ADR-0096's replay stub already carried the same
  value. **This is a patch, not a minor:** the event's digest matched no row, no record and no
  stub, so it identified nothing any consumer could look up; the field's name, presence and shape
  are unchanged, and a digest over a raw non-JSON fragment is unchanged too. A new integration
  assertion pins the equality in both cases.
- **A turn that completed with no text says so in the console** rather than rendering a blank
  cell. `content_cell` already separated a scrubbed body from a retained one; a genuinely empty
  body fell through to the retained branch and rendered as nothing, which beside `Finish: stop`
  and `Output: 12` reads as a broken render rather than as the fact it is. It now renders *no
  text*, muted, with the reason on hover. `domain.explanation.content_or_removed` already held the
  principle — an empty string is content, not an absence — and the explanation document was always
  honest; only the HTML was silent. This is the operator-visible half of the F2 decision below.

### Known limitations
- **An empty declared `stop` completes a step, and that is contract 6 working** (I2's verification,
  finding F2). A provider that declares `finish_reason=stop` and returns no text completes the
  step, because contract 6 is a *wire* contract — the declared reason decides, and nothing else
  does. Named here rather than changed, and deliberately so. Making the answer's text an input to
  the decision would let the model's output decide the harness's control flow, which is the one
  thing this suite refuses everywhere; "empty" has no single meaning across a text step and a
  structured one (`{}` is valid JSON and an empty plan, which I3 measured on `tools.plan`); and
  the retryable set at G3 excludes declared finishes on purpose. The honest surface is the
  explanation, not a halt — see the console fix above, which is what this row shipped instead.

### Changed
- **The two vendored LoadCoach snapshots are LoadCoach 1.1.1's**, re-copied from `2a7ac58`
  (`tests/contract/loadcoach_openapi.json`, `tests/contract/loadcoach_task_profiles.toml`) with
  their digests and `SNAPSHOT_SOURCE` strings re-pinned. Three things differ from the copies these
  replace. `docs/openapi.json` moves by `info.version` alone, `1.1.0` to `1.1.1` — no path, no
  schema and no request body changed. `task_profiles.toml` gains the `adapters.measured` profile
  (LoadCoach's H5/LA3 work) and a rewritten explanatory comment on `tools.plan` recording I3's
  `think` measurement (ADR-0099). No assertion in either contract test moved, and no profile this
  application's tiers name changed a value: `adapters.measured` is a profile no PromptCadence tier
  references, and the comment is inert to `tomllib`.

## [1.0.0] — 2026-09-06

**M12 — PromptCadence 1.0.** Every phase of the development plan is built, gated and demonstrated:
P8's compaction, explanation and console, and P9's hardening — the security checklist held item by
item, the prompt-injection corpus as a release gate, the retention sweep, every spec §15 budget
asserted, the remote tier reading its fact from LoadCoach, the operator set, the OpenAPI snapshot,
the generated configuration reference and a hashed CI lock. What ships known is under *Known
limitations*; the release handoff (`docs/history/I2_HANDOFF.md`) records the independent-brief
verification on a real LoadCoach 1.1.0 over Ollama, the ten §15 numbers, and the corpus inventory.

Of the beta's three known limitations: the sandboxed-tool clause of spec §20 #2 was closed at G2
(six declared calls on the real stack, all `ok`); the per-step retry was closed at G3; the podman
condition stays open below.

### Added
- **The read tools' descriptions name the configured `read_roots`** (I2's verification, finding
  F1). ToolYard's shipped text — *"a path outside the workspace is refused"* — is true of a plant
  with no read roots and misleading with them: on the real stack an obedient model refused to read
  the very directory the operator configured, and a bold one guessed the path. `read_file` and
  `list_dir` now say *"Also readable, by absolute path: …"* with each root; `write_file` does not,
  because its containment is the workspace alone. The description is caller-written prompt content
  and the corpus asserts the wire carries the registry's text verbatim, so the two cannot drift.
  With the sentence in place the same brief was delivered on the real stack (the handoff's run 4).
- **The two `[server]` limits are enforced** (P9, Security Standards §14). `rate_limit_per_minute`
  and `max_body_bytes` were declared, shown by `config show` and the example file, and read by
  nothing. `RateLimitMiddleware` (LoadCoach's, transcribed without the reverse-proxy half) applies
  a per-credential token bucket to `/api/v1` — `rate_limit_burst` (new, 100) at once, then the
  rate; `0` disables — and a failed-authentication brake per address (`failed_auth_per_minute`,
  new, 20). At the limit a caller gets `429 RATE_LIMITED` with `Retry-After`, never a dropped
  request; `/api/v1/version` is exempt. `BodySizeLimitMiddleware` answers `413 PAYLOAD_TOO_LARGE`
  before a byte over the cap is buffered; `SameOriginMiddleware` refuses a JSON write whose
  `Origin` names another host with `403 CSRF_FAILED`.
- **Every `/api/v1` route except `/version` resolves a principal** (spec §14). Until now only the
  explanation and the approval routes did; `POST /trajectories`, cancel, the reads, health, status,
  tools, the ledger and the egress decisions answered anyone once a token existed. Now `read`
  guards the reads, `write` the submit and the cancel, `approve` the grant and the denial. On an
  open loopback install nothing changes. Client-mode CLI commands present
  `$PROMPTCADENCE_API_TOKEN` on every call, not only `approve`/`deny`.
- **`tests/security/`** — Security Standards §14 item by item, each a named test, with a closing
  test that asserts every item held elsewhere still resolves to a callable.
- **The retention sweep, workspaces included** (P9, spec §14; migration `0011`). A terminal
  trajectory older than `[storage] content_retention_hours` loses its transcript text and
  `tool_calls_json`, its plan document and every step description, its tool arguments, result
  summaries and refusal details, its task, and its workspace directory; every digest, model
  identity, tier, usage figure, ceiling verdict, egress decision, deviation, approval and event
  stays, and the explanation is re-materialized from the scrubbed rows through `invalidate` so a
  scrubbed trajectory still explains itself. `trajectories.content_scrubbed_at` is the stamp that
  makes a second pass skip the first's work and says when. An in-flight trajectory is never swept,
  whatever its age. Runs from the worker at the lease-reap cadence; `retain_content = true`
  disables it, and its docstring no longer says the sweep "arrives later".
- **The prompt-injection corpus is a release gate** (P9, ADR-0095; `tests/security/
  test_injection_corpus.py`). Ten cases, each a hostile input plus a model-independent property
  of the harness: a plan description carrying instructions is quoted as data and moves no
  dispatch; `[tool_calls]`-shaped text executes nothing; a declared tool with hostile arguments
  never leaves containment; an invented name is refused although the model was told which tools
  exist; a tool result carrying instructions is replayed as a `tool` turn and changes no envelope;
  the offered definitions are the registry's verbatim; `http_fetch` refuses a link-local address,
  a cross-host redirect and an oversize body even on an allowlisted host; a plan that routes
  confidential work to a remote tier never reaches it; model output shaped like the record cannot
  change the record's structure; a compaction summary that emits a tool call executes nothing.
  None asserts what the model said.
- **`turn_overrun` is decided before `STEP_LIMIT_EXCEEDED` halts**, and a journey holds it
  (spec §13; G3's open question). A step reaching the intent's `max_turns` with no declared finish
  parks for the scoped re-approval lifecycle §5 specifies; the halt is for the bound nobody can
  extend by approval — `max_turns_per_step` round trips spent.
- **Replayed tool-call arguments are capped at ToolYard's record bound** (P9, ADR-0096 — G2's
  moved hazard). A call whose canonical arguments exceed `DEFAULT_MAX_ARGS_JSON_BYTES` (16 KiB)
  is replayed with the size-and-digest object ToolYard already wrote to
  `tool_call_records.args_json` at the same bound, id and name kept so the `tool` turn still
  answers it; the call itself ran with its full arguments, the rows keep them until the retention
  sweep, and nothing is truncated mid-string. One bound, one shape; the record and the wire agree
  by construction.
- **Every spec §15 budget is a `performance`-marked test** (P9, ADR-0097; `tests/performance/`).
  Ten rows: the median over 20 measured iterations after 3 warm-ups must not exceed the
  **ceiling**; median, p95 and the target are printed for the release handoff. Per-turn overhead
  and tool-dispatch overhead are separated from LoadCoach time and tool time by measuring at the
  transport boundary and solving two runs for two unknowns; the SSE row is measured over a real
  loopback socket, where the poll quantisation is visible; the 500-turn materialization and the
  100-trajectory recovery are built synthetically from rows. Excluded from the default gate.
- **The remote-provider fact is read from LoadCoach, never assumed** (P9, ADR-0098 — I13's
  recorded-transport half). `ModelEntry` and `ModelInfo` carry LoadCoach 1.1's `provider_name`
  and `is_remote`; the response's declared egress class is now the verification of the execution
  subject on a mixed registry (a kind the registry never named is `subject_unverifiable`), and
  the single-provider rule stays for a response that declares nothing. The loop and the approval
  service read the fact through `remote_provider_registered` — once per trajectory — instead of a
  constructor constant; the turn row records `model_provider_name`. **Found:** LoadCoach 1.1.0
  renders `is_remote` on the generate response but not in `GET /models`, so on that version a
  remote registration is invisible before its first turn and remote tiers stay
  `loadcoach_has_no_remote_provider`; the render is LoadCoach's to add.
- **`GET /tiers`** (spec §7.1, absent until now): every configured tier with its ceiling and
  availability — the report the console's Tiers page renders. A remote tier is unavailable for
  exactly one recorded reason, `loadcoach_has_no_remote_provider` or `unpriced`; the `tiers`
  health component, `doctor` and `tiers check` say the same in the same words.
- **The fake LoadCoach can play a mixed registry** (`remote_model=`): a task profile that allows
  remote providers is served by the remote registration, `/models` lists both, and the response's
  `model` block carries the registration's name and declared egress class. The vendored LoadCoach
  OpenAPI snapshot is refreshed to `93063bd` (1.1: optional `data_classification` and `adapter`).

### Known limitations
- **The live remote run (roadmap I13's second half) is deferred by ADR-0098.** Remote tiers refuse
  honestly until LoadCoach has a registration declaring `remote = true` and the tier is priced.
  The recorded-transport journey is in CI (`tests/integration/test_remote_tier.py`); the live
  run's command sequence is in `docs/tiers.md`.
- **`GET /settings` and `PUT /settings` (spec §7.1) are not served.** PromptCadence 1.0 has no
  runtime-changeable setting; every key is file-or-environment with a restart. Recorded in
  `docs/troubleshooting.md` rather than stubbed, so `docs/openapi.json` says what is served.
- **`pytest -m isolation -rs` on a real podman host** remains unexercised (outstanding-work §4):
  the reference machine has docker only, and `run_command`'s container rung has run through docker.

### Documentation and release plumbing
- **The operator set** in `docs/`: quickstart, the generated configuration reference (with
  `promptcadence config reference [--check]` and a test that fails on drift), tiers, security,
  operations, troubleshooting aligned with `doctor`'s four components, upgrading with the
  migration table and the 1.0 behaviour changes, and an index. `README.md` and `SECURITY.md` say
  what 1.0 is rather than what Phase 1 was.
- **`docs/openapi.json`** committed, with a contract test that fails when the API moves.
- **`requirements/ci.lock`** cut with hashes (pip-tools 7.6.1, Python 3.13) and adopted by every
  CI job but the 3.14 early warning: `pip install --require-hashes -r requirements/ci.lock`,
  then `pip install . --no-deps`. `pip-audit` runs against both locks. The clean-venv install
  from the lock and the beta-to-1.0 migration are proved and recorded in the release handoff.
- **A `.gitleaksignore`** for the one historical fingerprint: a fixture credential in the
  no-secret-in-logs sweep, since rebuilt as a low-entropy placeholder.
- **Context compaction** (P8, lifecycle §7). Before every turn the transcript is estimated against
  `threshold × context_budget_tokens`, and above it CutCtx plans a compaction over the configured
  `[compaction] policy_chain`. The threshold and the compaction target are **one figure**:
  compacting to the whole tier budget leaves no headroom and writes a `compactions` row per turn
  saying nothing changed. Compaction is a view, never a deletion — every original turn stays in
  `turns`, and the new `compactions` table (migration `0009`) plus its `context.compacted` event
  are the record that the wire differed from the rows.
- **`[compaction] policy_chain` is validated at startup.** An unknown policy name is a
  configuration refusal rather than a compaction that quietly skipped the policy an operator asked
  for.
- **The compaction summary is a governed turn** (ADR-0090, ADR-0091).
  It runs on the cheapest admissible **local** tier, under a superseding revision of the step's own
  intent — that tier, no fallbacks, no tools, the step's classification ceiling carried — and a
  second supersession restores the step's envelope. It is debited against every ceiling and does
  **not** count against `max_turns`, because that is the step's advance budget and a step must not
  be ended by its own housekeeping. It lives in its own thread (`compaction:<thread id>`), so it is
  never replayed into the transcript it replaced.
- **The `compaction.summarize` prompt record**, a versioned JSON record like every other
  (ADR-0012). The pack is `1.1.0`.
- **`COMPACTION_FAILED` has three producers and none is a traceback**: CutCtx's
  `BudgetUnsatisfiable` with both figures in `details`, the absence of any admissible local tier,
  and a summary LoadCoach could not serve.
- **The composed explanation** (P8, spec §11 contract 2): the
  `promptcadence.trajectory_explanation` `1.0` document, golden-tested, holding the request, the
  plan and its verdicts, every intent revision, every turn with its model, tier, usage and
  timings, every tool call, every debit with its ceiling verdicts, every egress decision, every
  deviation, every approval, every compaction and every event.
- **Materialized explanation revisions** (migration `0010`, ADR-0093). Composed
  once **after** the terminal transition, in its own write, into `explanation_revisions` plus an
  artifact under its digest. A missing revision is not a missing explanation: the live composition
  path serves it. `materialize(rows) == compose_live(rows)` byte for byte; dropping the whole
  table changes no answer; a rebuild over an intact cache writes nothing.
- **The invalidation entry point** (ADR-0092):
  `invalidate(trajectory_id, cause=…)` over `retention_scrub`, `recosting` and `schema_upgrade`.
  The retention **sweep** that will call it stays Phase 9's.
- **`GET /trajectories/{id}/explanation`** and **`promptcadence trajectory explain`**, answering
  the same document. Response headers say which path served it and under which revision; the body
  is the document, because a "which cache answered" field folded into it would make two reads of
  the same rows differ.
- **`promptcadence db rebuild-explanations`**, with `--drop` for the stronger form.
- **The operator console** (P8, ADR-0020): dashboard, trajectories, timeline, approvals inbox,
  tiers, tools, ledger, egress and system pages, server-rendered with progressive enhancement. The
  timeline renders the composed explanation document — the same bytes the API returns — so a
  record type that appears in one appears in the other. The one script shipped is a polite live
  region on an in-flight timeline; every read-only page is complete without it.
- **CSRF on every form** (ADR-0094). MirrorWall's double-submit token, wired because the inbox posts. The console authenticates
  exactly as the API does and adds no session cookie, which makes it loopback-first by decision;
  the System page says so and what changing it would take.
- **A step's attempts are on the record** (G3). `plan_steps.attempt` (migration `0008`) counts
  which attempt of a step is running or last ran, and a new `step.retried` event carries each
  attempt's number, the turn that was announced and never answered, its tier, its cause and its
  error code — written in the same write that starts the attempt (ADR-0044). The events are the
  history and the counter is a summary: the bypass loop's synthetic `loop` step has no
  `plan_steps` row, so an explanation reads attempts from the events and both paths' records stay
  identical.
- **`[execution] step_retries`** (G3, default `1`, `ge=0`), the per-step retry budget —
  configuration, not a constant, the `[planning] corrective_retries` precedent. `0` is one attempt
  and no repeat.
- **The model is told which tools it has** (G2). Every executing turn now carries the step's
  declared tool definitions on LoadCoach's `/generate` — name, registered description and argument
  schema, taken verbatim from the tool catalog so the wire and `GET /tools` cannot drift. The set
  offered is the **intent's allowlist and nothing wider** (lifecycle §4.3); a tool the intent did
  not declare is not offered, a tool configuration withheld is not offered, and a name the model
  invents anyway is still an `undeclared_tool` deviation, refused and recorded exactly as before.
  Until LoadCoach's wire could carry definitions (its G2 row), a model was told nothing and
  invented names out of its own vocabulary — every call refused, which is what G1 hit on the real
  stack.

### Changed
- **The trajectory stream polls every 2 ms, not 20** (spec §15, the added latency per SSE event).
  At 20 ms the measured added latency over a real socket was a uniform 0–20 ms, median 10 ms,
  against a 5 ms target; at 2 ms it is median 1.4 ms, p95 2.2 ms. LoadCoach's F12 figure, adopted
  at I2's interview.
- **`plans.raw_document` is nullable** (migration `0010`). A plan document is model output and
  must follow the retention scrub exactly as transcript text does; the column was `NOT NULL`, so
  the sweep Phase 9 will write could not have scrubbed it. Nothing writes `NULL` — a drafting
  attempt always records what came back, and `document_sha256` outlives the words.
- **Migrations run with SQLite foreign keys off**, restored afterwards — LoadCoach's `env.py`,
  copied rather than reinvented. Altering a column on SQLite is a table rebuild, and dropping a
  table that other rows reference `ON DELETE CASCADE` deletes those rows: without this, `0010`'s
  rebuild of `plans` silently deleted every `plan_steps` row.
- **A failed step repeats before it halts the trajectory** (G3, ADR-0076). A LoadCoach service
  failure that could plausibly answer differently — `ALL_CANDIDATES_FAILED`, `PROVIDER_TIMEOUT`,
  `QUEUE_FULL`, `RATE_LIMITED`, the client's own read timeout and their kind — is repeated on the
  same tier under the **same** `ExecutionIntent` revision, through the same pre-flight order, the
  same debit and the same comparison. Nothing is minted and nothing widens. A repeat comes
  **before** a tier escalation, which asks for a wider envelope and is unchanged; it stops the
  tier ladder rather than falling through it. Both paths, one code path. When the budget is spent
  the trajectory halts at T12 with the last attempt's error code, its cause naming every attempt
  with its tier.

  Not repeated: a governance outcome, which is structurally out of reach — egress, pricing,
  availability and budget run before the call site and every deviation is compared after it — and
  a deterministic refusal (`VALIDATION_ERROR`, `CONTEXT_LIMIT_EXCEEDED`, `CAPABILITY_UNSUPPORTED`,
  `FORBIDDEN`, and any code this build does not know). `NO_ELIGIBLE_MODEL` and
  `TASK_PROFILE_NOT_FOUND` keep their own mechanism. An unreachable LoadCoach still fails at T13:
  no backoff and no `waiting` state were added.
- **A tool-calling assistant turn replays natively; the `[tool_calls]` text rendering is gone**
  (G2). `_render_tool_calls` existed because LoadCoach's message body had no `tool_calls` and a
  provider refuses an assistant turn with neither content nor calls, so a turn that answered with
  calls alone was replayed as text naming them. It now replays as what it was. `turns.tool_calls_json`
  still persists what an assistant turn requested — that is the park-and-resume record and it is
  unchanged.

  A replayed `TOOL` turn now carries the **model's** call id on the wire, not this application's
  invocation ULID: LoadCoach refuses a tool result that answers no earlier call, and a provider
  matches results to calls by the id the model chose. The rows keep the ULID; only the wire is
  rewritten, positionally, in the order the calls were executed. A call the provider could not
  name is not replayable, so that call and the `TOOL` turn answering it are left off the wire while
  the rows keep both.

### Fixed
- **Tool-call fragments are grouped by `call_index`, not by `id`** (G2, found on the real stack).
  ModelRack's Ollama adapter emits one call as two deltas: the first carries the `id` and the
  `name` with no arguments, the second carries the argument text with **no id**. Keyed on the id,
  `assemble_tool_calls` made two calls out of one — a named call with empty arguments, refused
  `args_invalid`, and a nameless call with the arguments, refused `unknown_tool`. That pair is in
  G1's §10.4 transcript, where it was read as the model inventing names; part of it was this
  function. `call_index` is ModelRack's own answer to *which call is this a fragment of*, so it is
  what the assembler groups on, with `id` and then position as fallbacks.

## [0.9.0b0] — 2026-09-04

**The M11 beta.** Cut by operator decision after G1's live demonstration on a real LoadCoach over
Ollama with the shipped defaults: a bypassed and a (tool-free) planned trajectory completed, their
records identical minus the plan rows by the contract-1 diff, and a `confidential` trajectory was
refused before any request reached a remote tier. What did **not** pass, and ships known, is under
*Known limitations* below.

### Added — Phase 7, gate E: the approval, tier and token surfaces
- **HTTP:** `GET /approvals` (the pending requests — `kind`, the step ids, the ask, and for a
  `ceiling_raise` the proposed budget), `POST /approvals/{id}/approve` and
  `POST /approvals/{id}/deny` (idempotent; a decision on a resolved request is
  `APPROVAL_INVALID_STATE`, 409); `GET /trajectories/{id}/plan` (every drafting attempt with
  `valid` and its issues, and the approved plan) and `GET /trajectories/{id}/intents` (every
  revision, `minted_by`, the gate); `GET /system/status` is real — the pending approval requests,
  the active trajectories and the ledger position, no placeholder.
- **CLI:** `promptcadence approvals list|approve|deny` (the token from `PROMPTCADENCE_API_TOKEN`,
  now a reserved name); `promptcadence tiers list|show|check` — `check` asks LoadCoach whether
  every configured tier's profile and `tools.plan` resolve, and exits 4 when one does not;
  `promptcadence token create|list|revoke`; `--follow` ends on `plan.rejected` as it does on the
  terminal events; `doctor` gains a `tiers` component.
- **Authentication, the minimum the `approve` scope needs (spec §14):** bearer tokens stored as
  SHA-256; scopes `read`, `write`, `approve`, `admin` (`admin` grants all; `approve` is
  deliberately not `write`); `UNAUTHORIZED` 401, `FORBIDDEN` 403, `TOKEN_NOT_FOUND` 404. A
  loopback bind with no active token is open and records its decisions as `approver:loopback`
  (LoadCoach's precedent); a non-loopback bind with no token refuses.

### Fixed — Phase 7, gate F: what the real stack forced (gpt-oss:20b under `tools.plan`)
- The corrective never replays an empty answer as an assistant turn (ModelRack refuses a turn
  with neither content nor calls), and the validator names an empty document as such: *a model
  that spends its output budget thinking returns nothing*. A planning call that fails with a
  LoadCoach error cancels every planning job under the trajectory's key prefix — LoadCoach's own
  corrective retry refuses its own request in exactly this case and leaves the job `executing`.
- A LoadCoach **validation** failure on a drafting call (`VALIDATION_ERROR`, `VALIDATION_FAILED`,
  `STRUCTURED_OUTPUT_INVALID`) is recorded as an empty attempt whose issue names the code, and
  `corrective_retries` decides whether to try again (spec §13's planner row). Unavailability and
  every other code still propagate.
- **`planner.draft` 1.1.0** — the field list is the schema and a tool is one sentence (1.6k
  characters, from 4.4k). Shown the schema block, the model returned an empty document on every
  try. `PLAN_SCHEMA` and the five rules are untouched; ADR-0041 never required the schema to be
  shown.
- An assistant turn that answered with tool calls alone is replayed as text naming the calls,
  read from its row — LoadCoach's message body carries no `tool_calls`, so it could never be
  replayed natively and the next turn failed. The row keeps the empty content the model produced.

### Build
- **Release plumbing, which had never existed in the shape the suite uses.** `release.yml` gains
  the `pypi` environment (the trusted publisher's, so a tag push waits for the operator's one
  approval), a manual `workflow_dispatch` TestPyPI dry run (Packaging and Release Standards §6
  requires one ahead of a package's first release, and 0.9.0b0 is this package's first), and the
  hash-pinned build chain: `requirements/release.in` and `requirements/release.lock` (`build`,
  `hatchling`, `twine`; byte-identical to LoadCoach's and LoadLedger's, which is the
  reproducibility check) installed with `--require-hashes` before `python -m build
  --no-isolation`, in both jobs, so the artifact the dry run proved is the artifact the tag
  builds. CI still installs from ranges; a `ci.lock` is a separate change.

### Known limitations
- **No model-directed sandboxed tool call has succeeded on the real stack**, and none can until
  LoadCoach's `/generate` carries tool definitions and `tool_calls` (outstanding-work **G2**). A
  model is never told which tools exist, invents names, and every call is refused and recorded —
  contract 1 holds; spec §20 #2's sandboxed-tool clause is exercised against the fake only.
- A failed step halts the trajectory; per-step retry is scheduled (**G3**).
- `pytest -m isolation -rs` green on a real podman host is an M11 exit condition this release
  does not carry (outstanding-work §4).

### Added — Phase 7, gate B: approval in three modes, and minting as its output
- **`auto`** mints every approved step by policy in the write that approves the plan (T4).
  **`manual`** holds every plan on one `plan` request (T5) — **and holds a bypassed trajectory
  too** (§0.2(5) of the kickoff, ADR-0048/ADR-0049 rule 3): the default intent's gate is
  evaluated at its minting, and when the mode requires a person the claim continues from T3
  straight into T10 on a `bypass_gate` request before any turn runs. The grant **supersedes**
  the default intent — revision 2, `minted_by = approver:<token id>`, `approval_request_id`
  set, revision 1 retained as the gated envelope nobody executed under — so *"the human grant,
  when one gated it"* (lifecycle §4.3) is in the record. A test asserts, over every scenario,
  that no turn ran under an intent whose gate fired and whose grant is not in the record.
- **`hybrid`** mints the ungated steps and parks **at the point a gated step becomes ready**
  (T10, `gated_step`), after the ungated ready work has run; when nothing ungated can start it
  parks from planning (T5). Gates are evaluated against the most permissive tier in the intent's
  set, so a `local_*` primary with a `remote_*` fallback gates as remote — tested on both
  minting paths a fallback can arrive by.
- **`approval_requests`**: exactly one pending per trajectory (a second is refused before it is
  written), the `request_timeout_hours` clock persisted as `expires_at` and read by the worker
  on every pass — expiry is T9 with the timeout recorded, never a grant. Grants and denials are
  **idempotent per request**; a resolved request cannot be resolved differently
  (`APPROVAL_INVALID_STATE`). A denial halts with `APPROVAL_REQUIRED`, the denier's reason on
  the row and `approval.denied` + `trajectory.halted` in one write.
- **Scoped re-approval is real** (spec §20 #8): a drift whose disposition is `scoped_reapproval`
  parks on a `reapproval` request scoped to **that step**, carrying exactly what the drift asked
  (`ReapprovalAsk`: the tools, the next tier, the turn or budget extension, the observed
  classification); the grant mints revision *n+1* superseding *n*, both retained, `supersedes`
  set, and the step's pending tool calls then run under the widened envelope. A tool outside the
  *trajectory* allowlist stays refused and never re-approvable — F2's test still passes.
- **`NO_ELIGIBLE_MODEL` and an unavailable tier fall to the intent's next tier**, else raise a
  `tier_escalation` deviation whose scoped re-approval carries the next tier in the escalation
  order, or halt `TIER_UNAVAILABLE` naming an exhausted order (spec §13's cell). More than three
  deviations on one step halts `DEVIATION_HALTED` (lifecycle §5).
- **A ceiling raise is a grant with a budget**: `on_exhausted = "approval"` parks on a
  `ceiling_raise` request naming the refusing scope; the grant carries the new per-trajectory
  ceiling, moves the row's ceiling and mints a superseding revision. The per-day and per-project
  ceilings are configuration and a grant cannot move them; the request says which scope refused.
- **Tokens and the `approve` scope** (spec §14, ADR-0026, ADR-0049 rule 2): `services/tokens.py`
  (create, list, revoke; SHA-256 stored, the secret shown once; four scopes as a **set**, `admin`
  containing the rest, `approve` deliberately not `write`) and `web/auth.py` (`resolve_principal`,
  `require_scope`). Loopback with no tokens is open and names itself — a grant from it is
  recorded as `approver:loopback`; once a token exists a bearer is required (`401`) and checked
  (`403` without the scope). `bootstrap`'s manual-mode refusal now accepts an `admin` token.
- `VerdictReason.SCOPED_REAPPROVAL`; `gate_reason` is public so the bypass gate's request names
  its reason in the same vocabulary a planned step's does.

### Added — Phase 7, gate C: ready-set dispatch over the plan DAG
- **`domain/dispatch.py`** — the pure rule (lifecycle §8.4): a step is ready when every
  dependency has committed; `max_concurrent_steps = 1` dispatches one at a time; above 1,
  concurrency is granted only across **disjoint surfaces** — at most one local step in flight
  ever (ADR-0038), up to `max_concurrent_remote_steps` remote ones. Walked as a matrix.
- The loop dispatches from the ready set, records the DAG on `plan_steps.depends_on_json` and on
  every `step.started` event even when execution is serial, frames each step with the results of
  the steps it depends on, and runs chosen steps together through a thread pool when the rule
  allows — every write still fenced on the one lease.
- **Multi-step reconciliation**: a takeover mid-step resumes at that step's own thread (no
  duplicate thread, no duplicate `step.started`), cancels the orphaned job and continues; the
  kill −9 machinery is D2/F1's, extended rather than rebuilt.

### Added — Phase 7, gate D: the governance-invariance diff (spec §11 contract 1, I11)
- **`tests/contract/test_governance_invariance.py`** runs one scripted task twice against the
  fake — planned and `bypass_planning` — into two fresh databases, reduces every table's rows to
  shapes (identifiers, timestamps and digests masked, everything else by value) and diffs them.
  The permitted-difference set is **named in the test, one item at a time, each with the
  document that permits it**: the plan tables; the `plan.*` events and T2-vs-T3's `state` on
  `trajectory.claimed`; the one `step.execute` framing turn and the counts it shifts by exactly
  one (asserted as a relation, not masked); `minted_by`, `step_id` and the **slice** fields of
  the intent (the bypass default's slice *is* the trajectory's ceiling and turn cap, ADR-0056
  §2; a planned step's is its estimate × 2); `threads.step_id`; and the request's own
  `bypass_planning` flag. Every governed field, the ledger, every egress decision, every
  deviation, every tool-call record and the terminal transition are identical, and a second
  scenario raises the same deviation on both paths. The failure message names the table, the
  row and the field that moved.
- **The structural half**: an AST walk asserts `LoadCoachClient.generate` is reached from
  exactly two sites — the loop's `_call`, which takes a `_StepRun` (an intent and its thread),
  and the planner's `draft`, which produces no turn — so there is no third place a model could
  answer outside an envelope.

### Added — Phase 7, gate A: the planner
- **`services/planner.py`** — `Planner` calls LoadCoach under the shipped **`tools.plan`**
  profile with `response_format = "json"`, validates the answer with PromptCadence's **own**
  `validate_plan_document` (ADR-0041: the schema is shown to the model in the prompt and never
  handed to LoadCoach for validation), and retries correctively within the new
  **`[planning] corrective_retries`** budget (default 2), feeding **every** issue back at once
  (`plan.py`'s module docstring explains why one problem per attempt spends the budget on
  bookkeeping). Exhaustion is **T7** with `PLAN_DRAFT_FAILED`, the cause naming every attempt's
  issue reasons. A fenced or otherwise non-JSON answer is an issue the corrective names, never a
  document this application repairs.
- **`src/promptcadence/prompts/`** — the application's prompt pack on `setspec.prompts`
  (ADR-0012, ADR-0028): `planner.draft`, `planner.corrective` (the structured-output corrective,
  spec §9) and `step.execute` (the framing turn a planned step's thread opens with), a manifest
  pinning their hashes, and `services/prompts.py` as the one-function shim. A test walks the
  source for inline prompt strings; another rebuilds the manifest and asserts nothing drifted.
- **Every drafting attempt is a `plans` row** — valid or not — carrying the verbatim document,
  the validated form (or every issue), the planning call's job, subject, four token classes and
  timing, and the prompt record it was rendered from. `plan.drafted` is emitted once per
  attempt. The planning call's spend is recorded on the row and **not** debited to the ledger:
  it is not a turn and runs under no intent, and contract 1 says debits occur under an intent on
  every turn in both modes.
- **`services/intents.py`** — intents as rows, and rows back into intents **by re-minting**:
  `rebuild_intents` re-mints every recorded revision from the recorded inputs (the declaration,
  the plan step and its verdict, the tier snapshot; every later revision by superseding the one
  before it with the fields its row carried) and refuses to run a trajectory whose envelope it
  cannot reproduce byte for byte. `step_budgets` is lifecycle §5's one multiplication (estimate
  × 2), in one place so the mint and the re-mint agree.
- **`services/approvals.py`** — the verdict as rows: `decide_plan` records `plan_approvals`
  with the **derived** `approval_policy_version`, then does what the mode says in the loop's own
  write — T4 (mint every ungated step), T5 (hold), T6 (reject, every step's verdict and the
  binding ceiling in the cause). Grants, denials and expiries are gate B's.
- **The planned path in `services/loop.py`** — T2 claims for planning; `run()` drafts, records,
  approves and then executes per step: one thread per step (`threads.step_id`), the caller's
  task verbatim as turn 1 and the `step.execute` framing as turn 2 with its prompt provenance on
  the row (spec §9), `step.started`/`step.completed` on **both** paths (the bypass loop is one
  synthetic step), and T11 in the write that commits the last step. The bypass path's thread now
  opens at the step's first dispatch, not at the claim — the same shape as a planned step.
- **The `planning` recovery edge is real** (lifecycle §8.3): a `planning` lease found at
  recovery re-claims, cancels every in-flight planning job under the trajectory's key prefix,
  emits `trajectory.recovered` and redrafts under a fresh session nonce. The stub D2 left —
  *"planning is not available before Phase 7"* — is gone from code, tests, CLI output and docs,
  and a test greps for it. Recovery now takes the lease **before** cancelling an in-flight job,
  on both edges: a stalled worker whose call returned the cancelled document could otherwise
  end the trajectory itself in the instant before the takeover fenced it.
- **Migration `0007`** — `threads.step_id`; `turns.prompt_id/prompt_version/prompt_sha256` and
  `turns.tool_calls_json` (the calls an assistant turn requested, so a step resumed after a park
  or a crash runs them instead of losing them); the drafting-attempt columns on `plans`;
  `plan_steps.status/started_at/completed_at`; `approval_requests.kind/detail_json/
  resolution_reason`; and `deviations.turn_id` loses its foreign key — a `tier_escalation`
  names a turn that was announced and never answered.
- `PlanDrafted`, `StepStarted` and `StepCompleted` event bodies; `domain/dispatch.py` with the
  pure ready-set rule (gate C exercises it).

### Changed
- `GET /trajectories/{id}/turns` and `TrajectoryService.turns()` return every step's thread in
  the order the threads opened; each turn document carries `step_id` and the three `prompt_*`
  fields.
- Spec §12 gains `[planning] corrective_retries`; lifecycle §8.3's last sentence — the
  Phase 7 placeholder — is replaced by how a planning job is known and cancelled at recovery.

### Changed
- **`promptcadence ledger show --scope tier` and `GET /api/v1/ledger`'s `tiers` array report
  spend, not a debit count.** `loadledger 0.2.0` added `balances(scope, window_key)` — a read of
  one window that names no run and reads through no ceiling — so a tier, which has no cap over it
  by design (lifecycle §6), now has a balance the ledger can be *asked* for. Each tier carries its
  tokens, its per-currency money, the three honesty counts and the rendered strings beside the
  numbers, exactly as a ceiling's headroom does. **No arithmetic was added to
  `services/budget.py`**: it asks, renders and returns.
- Tier money is a **list**, one entry per currency, plus a single `money_spent_display` for a
  surface that prints a line rather than a table. A window's currency set is open and figures are
  never summed across it (ADR-0030 rule 3), so one total would be a conversion. A tier that has
  priced nothing renders `—`, never `$0.00` (ADR-0016), and a floor renders "at least" through
  `render_money` — the **spent** renderer. `render_remaining_money` is not used here and must not
  be: it qualifies with "at most", because a cap less a floor is an upper bound, and there is no
  cap here to be under.
- The CLI's closing line for `--scope tier` was *"no tier ceiling is configured, so a tier has a
  history and not a balance"*, which is no longer true. It now says these are balances and not
  headroom — nothing here can be exceeded.
- **`GET /api/v1/ledger` names no run.** `BudgetService.ledger_view` lost its `reference_run`
  argument and `_report` now calls `Ledger.position()`, the ledger-wide counterpart of
  `remaining()`. Previously both surfaces passed an arbitrary known trajectory to satisfy a
  signature — sound, since a `per_day` or `per_tag` window is ledger-wide, but a workaround, and
  on an empty ledger it fell back through `UnknownRun` to a fabricated "nothing spent". The
  figures are unchanged; they are now facts rather than a fallback. `LedgerView.day` is therefore
  no longer optional.
- Dependency floor raised to **`loadledger[sql]>=0.2,<0.3`**. The floor moved with the surface:
  `ledger_view` calls `balances()` and `position()`, neither of which exists in 0.1.0, and a floor
  below the surface an application actually calls fails at runtime as an `AttributeError` instead
  of at install time as a resolution error.

### Removed
- `TrajectoryService.most_recent_id()`. It existed only to supply the reference run the two ledger
  surfaces no longer need, and its docstring described a workaround that no longer exists.

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

- **Phase 5, gate D: the estimator's source is recorded, and a model can never be one.** The
  ladder is historical p80 per token class over `entries()` at or above `estimate_min_samples`,
  else the tier's configured per-step default — with the source label and the sample count on
  every estimate either way (lifecycle §6, ADR-0047 §4). The threshold is tested on both sides of
  20, because a test of one side passes on `>` as readily as on `>=`.
  - The estimate is over observed **usage**, and the money it implies is derived by pricing it —
    the same operation, against the same record, that costs a real turn. An estimator that stored
    money would be a second place a money figure lived (ADR-0030 rule 1).
  - `StepEstimator.estimate` takes a tier name and nothing else, and the module does not import
    the LoadCoach client at all; both are asserted, because a model-generated number would arrive
    as an innocuous-looking parameter rather than an obviously wrong one (D-3).

- **Phase 5, gate E: a crashed debit is reconciled exactly once.** The debit is written **before**
  the turn row and in its own transaction, so a crash that loses the turn cannot leave spend
  recorded for a turn that never existed, and a crash before the debit leaves a turn row that
  recovery re-derives it from. `LoopController.reconcile_debits` is idempotent by `source_ref` and
  runs at the head of every reconciliation pass; it also accounts for a database migrated into
  Phase 5 with turns already in it, which are real spend the ledger never saw.
  - Proved by running it: the existing `kill -9`-between-response-and-debit test now asserts the
    spend appears exactly once keyed by the reconciled turn's own id, and a second recovery writes
    nothing and leaves every entry byte-identical.

- **Phase 5, gate F: the surfaces.** `GET /ledger` reports today's position against the per-day
  ceiling and each configured project's, plus one trajectory's when asked; `GET /ledger/entries`
  returns recorded debits newest first, filterable by trajectory or tag. `promptcadence ledger
  show [--scope day|project|tier|trajectory]` is mode **either** — it asks the server when one
  answers and reads the database directly when none does.
  - Every money figure crosses the boundary twice: as `{currency, nanos}` for a caller that
    computes and as a rendered string for one that displays, so a floor reaches a UI qualified and
    an unpriced amount as `—`, rather than each surface inventing its own way to show them. The
    qualifier depends on the direction: a **spend** derived from a floor is `at least 0.004 USD`,
    while what is **left** is `at most 20 USD` — the opposite bound, because the cap less a floor
    is an upper bound, and "at least" there would reassure in exactly the case where less headroom
    may remain than the number says.
  - A refused pre-flight says so in those terms — "the tokens cap cannot admit it — counting this
    step the cap is over by 1 120" — rather than "the cap is spent", which was wrong on the common
    case of a ceiling too small to admit the very first step, where the ledger is still empty.
  - `--scope tier` reports debit **counts**, not balances: no tier ceiling is configured
    (lifecycle §6), LoadLedger reports a balance only through a ceiling, and summing entries here
    would put ledger arithmetic in an application. Recorded as a LoadLedger row rather than worked
    around.
  - The live journey now asserts the debits and the running balance a real run leaves behind, and
    still passes on the fake provider with no GPU, no Ollama and no network (spec §20 #10).

### Added
- **Phase 6, gate E (cont.): `--denied-only`, and acceptance criterion 1 over the prior phases.**
  - `promptcadence egress list --denied-only` is spec §7.2's shipped flag and is kept as a
    shorthand for `--verdict denied`; `--verdict` ships beside it because the vocabulary has three
    members and a boolean cannot ask for a `violation`. The two contradicting each other is
    refused rather than resolved by precedence.
  - Acceptance criterion 1 is demonstrated over **Phase 4's** multi-turn tool journey and
    alongside **Phase 5's** ledger, not only over a new single-turn test: the tier decisions and
    the assistant turns come out equal in count and name the same turns, and the egress decisions
    and the budget debits name the same turns as each other.

### Added
- **Phase 6, gate E: the surfaces.** `GET /egress-decisions` and `promptcadence egress list`
  (mode: either), plus the fetch tests the plan names.
  - Both list **approvals, denials and violations together and unfiltered by default**. A surface
    showing only refusals would answer "what was blocked" rather than "where did this
    trajectory's data go", and the second is what spec §11 contract 3 exists to make answerable.
  - Each row is SetSpec's `governance.egress_decision` 1.0, rendered from
    `EgressDecision.to_payload()` rather than from a projection kept in step by hand — a
    hand-written one would be a second definition of the payload (ADR-0051 §4). `--json` prints
    those documents unchanged.
  - `?verdict=` outside the vocabulary is **refused** (`400 VALIDATION_ERROR`), not ignored: a
    caller who asked for `verdict=blocked` and got everything back would read an unfiltered list
    as a filtered one, which on this endpoint means reading approvals as denials.
  - The fetch tests cover all three target shapes against the shipped policy: a non-allowlisted
    host denied `no_ceiling_declared`, an allowlisted host above the declared ceiling denied
    `classification_exceeds_ceiling`, and loopback approved `target_not_remote`. They run through
    an injected transport, so the suite still opens no socket.

### Added
- **Phase 6, gate D: the deviation matrix, as the bypass path actually mints it.** The comparison
  machinery was already wired end to end at Phase 4; what this adds is the evidence that it is
  wired against the *default* intent, not only against a hand-built one.
  - `tests/golden/deviation_matrix_bypass.json` renders every lifecycle §5 category, its severity
    and its disposition under both `reapproval_scope` values, over the intent
    `mint_bypass_default` produces from `TierPolicy`. All six categories are asserted present, so
    a golden that silently stopped covering one still fails.
  - This is the baseline Phase 7's contract-1 invariance diff is written against: when a planner
    mints the intent instead, these rows must not move — only `intent_id` and the `minted_by`
    kind may.
  - A tool outside the *trajectory* allowlist is asserted `refused_not_reapprovable` under **both**
    scopes, which is what makes "never re-approvable" a claim about the policy rather than about
    one code path.

### Added
- **Phase 6, gate B: every turn and every `NETWORK` tool call carries a recorded egress decision.**
  Commissioner renders the verdict and records it; enforcing it is this application's (ADR-0054).
  - `services/egress.py` — `EgressService.evaluate` has no path that decides without recording, so
    an approval is exactly as durable as a denial (spec §11 contract 3). Two evaluation points,
    one policy: `tier_target` for a turn, `fetch_target` for an `http_fetch`.
  - The classification is always the **trajectory's declaration**, never model text (spec §14). A
    model can influence which target it asks for; it can never influence how sensitive the data is
    said to be.
  - **Defaults closed** (ADR-0046): a non-allowlisted or unparseable fetch host is given *no*
    ceiling, so the shipped policy denies it with `no_ceiling_declared` — a recorded, queryable
    refusal rather than an unlogged early return. New `[tools] fetch_max_data_classification`
    declares the ceiling for allowlisted hosts; absent by default, which denies every non-loopback
    fetch. Loopback is `remote=False` and approves with `target_not_remote`, because a fetch that
    does not leave the machine is not egress.
  - **`http_fetch` is registered**, egress-checked, and the `egress_governance_deferred_to_p6`
    withheld cause is gone from the code, the catalog, `GET /tools`, `promptcadence tools list`
    and `doctor`. A denied decision leaves the invocation's `max_egress` at `NONE` and ToolYard
    refuses with `egress_not_permitted` — a structured result on the ordinary recorded path, not
    a second refusal path. Its transport and resolver are injectable, so the suite still opens no
    socket (spec §20 #10).
  - **The four pre-flights are now ordered, and the order is the guarantee**: egress, then
    pricing, then availability, then budget — all before `turn.started`, which is what makes
    "refused before any HTTP request leaves" a property of the code's shape. `TierRouter.resolve`
    splits into `tier_of` and `ensure_available` so a tier's *configured* egress class decides
    governance and its *availability* does not: without the split, a confidential trajectory aimed
    at a remote tier would be refused today for `loadcoach_has_no_remote_provider` and only for
    the real reason once LC-E1 registers a provider.
  - Spec §20 #5's unpriced refusal checks for a **`ModelPricing` record claiming the instant**,
    not for the `pricing_file` field: startup validation already refuses a remote tier naming no
    file, so a field check could never fire. An expired or empty price list is what reaches it.

### Added
- **Phase 6, gate A: Commissioner's table is mounted.** `commissioner[sql]>=0.1,<0.2` is a runtime
  dependency, and this is the second package-table mount here — a transcription of Phase 5's, not
  a second design (ADR-0050).
  - `infrastructure/db/models.py` — `EGRESS_TABLES = mount_egress_tables(Base.metadata)` at module
    import, unconditionally, for the reason `LEDGER_TABLES` documents: autogenerate only sees what
    was mounted before it inspected the metadata, so a lazy mount yields a revision that *drops*
    the package's table. The prefix stays the package default `egress_`.
  - Migration `0006` creates `egress_decisions` and its two indexes. Tests prove `alembic upgrade
    head` from empty equals the mounted shape column-for-column, that the index names match the
    mount, that `downgrade` removes it and leaves `0005`'s tables alone, and that the migrated
    schema leaves no pending autogenerate diff against `Base.metadata`.
  - Reads and writes go through `commissioner.sql.SqlEgressLedger`; nothing queries the mounted
    `Table` handle, which is kept only so the parity test can assert the shape.
  - **This pin decides the resolved `setspec`.** Commissioner requires `setspec>=0.5,<0.6`, so the
    environment now resolves `setspec 0.5.0` where it resolved 0.6.0 an entry above. That is
    intended, costs nothing today, and is recorded because it will need lifting before
    PromptCadence can adopt a 0.6 payload.

### Changed
- **`setspec` widened to `>=0.5,<0.7`** (E5's pin sweep). The old pin was `>=0.4,<0.5` with a
  comment explaining that it could not move because `mirrorwall 0.2.1` required `setspec<0.5`;
  `mirrorwall 0.2.2` lifted that cap, so the comment was stale and is deleted rather than amended.
  The floor is **0.5, not 0.4**, because spec §5 requires `governance.egress_decision` and setspec
  0.5 is where it appears — a floor below a component's own stated spec is drift that stays
  invisible until someone installs the wrong thing. Resolves to `setspec 0.6.0` today; when Phase 6
  adds `commissioner`, whose own pin is `>=0.5,<0.6`, the resolved version becomes 0.5.x. Nothing
  is imported that was not imported before: this widens a range and adopts no payload.

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

