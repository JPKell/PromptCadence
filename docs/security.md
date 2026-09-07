# Security

PromptCadence concentrates the two riskiest behaviours in the suite — executing model-directed
tool calls, and sending data to paid remote providers — so its posture is the strictest (spec
§14). This page is the exposure path end to end, in the order things are checked, and the
model-output boundary the injection corpus holds.

## The default: loopback, open

Bound to `127.0.0.1:8768` with no tokens, PromptCadence is open: the operating-system user
boundary is the security boundary (ADR-0014). The principal is `loopback`, holds every scope, and
its grants are recorded as `approver:loopback` — the record still says who. Every request still
passes Host validation — `localhost`, `127.0.0.1`, `[::1]` and the bound address — so a page
elsewhere cannot reach it through DNS rebinding.

## Exposing on a LAN

Three things, and startup refuses without all of them:

```toml
[server]
host = "192.168.1.10"                 # the interface, not 0.0.0.0
allowed_hosts = ["cadence.local"]     # every Host header you will accept (ADR-0026 §1)
allow_lan_exposure = true             # only if host is 0.0.0.0
```

```bash
promptcadence token create ideapress --scope write        # printed once; only its SHA-256 is stored
promptcadence token create ops --scope read,approve
promptcadence serve
```

* A non-loopback `host` without `allowed_hosts` is `INSECURE_BINDING` at startup.
* A non-loopback `host` with no active token is `INSECURE_BINDING` at startup.
* `0.0.0.0` without `allow_lan_exposure = true` is `INSECURE_BINDING` at startup.
* `[approval] mode = "manual"` with no `approve`-scoped token is `INSECURE_BINDING` at startup —
  a mode nobody can satisfy is a configuration error, not a runtime surprise (ADR-0049).

TLS is a reverse proxy's job (ADR-0014 §7); PromptCadence speaks HTTP. This release has no
`trusted_proxies` setting: the failed-authentication brake keys on the socket address, so behind
a proxy every client shares one brake. That is the loopback-first ceiling ADR-0094 names.

## Tokens and scopes

`Authorization: Bearer <token>`. Scopes are a **set**, not a ladder; only `admin` contains the
others:

| Scope | Grants |
|---|---|
| `read` | health, status, tiers, tools, trajectories, turns, plans, intents, explanations, the stream, approvals list, ledger, egress decisions, the effective runtime settings (`GET /settings`), every console page |
| `write` | submit and cancel a trajectory |
| `approve` | grant or deny a pending approval request — deliberately **not** part of `write`, so the identity that submits work cannot approve its own egress (ADR-0049 rule 2) |
| `admin` | everything above; **changing** runtime settings (`PUT /settings`, the Settings page's form) and tokens. Reading the effective settings is `read`: seeing what the process runs on is not the privileged half (ADR-0100) |

Once any token exists, or the bind is not loopback, **every `/api/v1` route except `/version`**
resolves a principal: `401 UNAUTHORIZED` without a usable token, `403 FORBIDDEN` without the
scope, `details` naming the required scope and the scopes held. `GET /api/v1/version` answers
without a credential so a client can negotiate before it knows whether its credential is right
(ADR-0026 §5). The CLI's client-mode commands present `$PROMPTCADENCE_API_TOKEN` (or `--token`).

Tokens are 256 bits of CSPRNG output, shown once, stored as `sha256:<hex>` and compared
constant-time. `promptcadence token list` never shows them; `promptcadence token revoke <name>`
makes one a 401 at once. A failed authentication is logged with the address and request ID, never
the token, and an address is braked after `failed_auth_per_minute` failures.

## The console

The console authenticates exactly as the API does and adds no session cookie (ADR-0094). A
browser sends no bearer header, so once a token exists — or the bind is not loopback — every page
answers 401 and names `promptcadence token create`. That makes the console **loopback-first by
decision**: a browser-usable off-loopback console needs login, rotation, fixation defence, logout
and idle expiry, which is its own record with its own threat model. The System page says which
mode the install is in.

## Forms, origins, bodies, limits

* Every form carries MirrorWall's double-submit CSRF token; a forged or mismatched post is
  `403 CSRF_FAILED`.
* A JSON write whose `Origin` names another host is `403 CSRF_FAILED`; CORS is disabled, so no
  browser page elsewhere can reach the API. Scripts and the CLI send no `Origin`.
* A body over `server.max_body_bytes` (1 MiB) is `413 PAYLOAD_TOO_LARGE` before it is buffered.
* Per-credential rate limit on `/api/v1`: `rate_limit_burst` (100) at once, then
  `rate_limit_per_minute` (600) sustained; `429 RATE_LIMITED` with `Retry-After` at the boundary,
  never a dropped request. `/api/v1/version` is exempt; the console is outside `/api/v1`.

## Model output is untrusted input

Tool names, tool arguments, tool results and plan content originate from a model and may be
adversarial. The properties below hold **whatever the model asks**, are decided by Python from
facts the model does not control, and are held by the prompt-injection corpus in
`tests/security/test_injection_corpus.py` — a release gate that asserts the harness and never the
model (ADR-0095):

* Only registry-listed, intent-approved tools are callable. The model is told which tools exist
  (the step's declared allowlist, no wider, descriptions verbatim from the registry); an invented
  name is refused `unknown_tool`, a registered tool outside the envelope `not_approved`, and both
  are recorded as an `undeclared_tool` deviation.
* Arguments are schema-validated then sandbox-checked: every filesystem tool operates under the
  trajectory's workspace with symlinks resolved before the check; a path escape, an absolute path
  outside containment and a `..` are refused, a shell metacharacter in a filename is a filename.
* `run_command` executes under ToolYard's tiered isolation — container → bwrap → **refuse**. With
  neither rung the tool refuses with `isolation_unavailable`; nothing runs unisolated. `doctor`
  and `tools list` show the rung this host has.
* `http_fetch` obeys ADR-0026 §3 on top of the egress decision: `file://` and hostless URLs,
  link-local addresses, cross-host redirects and oversize bodies are refused even on an
  allowlisted host, and a host outside `fetch_allowed_hosts` is refused by the egress decision
  before ToolYard is reached.
* Egress is evaluated from the **trajectory's** declared classification, never from model text.
* A tool result re-enters the transcript as a `tool` turn — never as a system turn, never as a
  tool definition — and changes no envelope.
* What goes *back* to the model is bounded: a replayed call whose arguments exceed ToolYard's
  16 KiB record bound carries the record's own size-and-digest object instead (ADR-0096).
* Model text reaching the console is autoescaped under `StrictUndefined`, and no template renders
  raw HTML from a record; model text reaching the explanation document lands inside
  `content.text` and cannot change the document's structure.
* A compaction summary runs under an intent that approves no tool; a tool call it emits is never
  executed.

## Retention

A terminal trajectory keeps its transcript text, plan document, tool arguments, task and
workspace directory for `storage.content_retention_hours` (24) and then loses them; every digest,
model identity, tier, usage figure, ceiling verdict, egress decision, deviation, approval and
event stays, and the explanation is re-materialized from the scrubbed rows. An in-flight
trajectory is never swept. `storage.retain_content = true` keeps everything (config-only).

## What PromptCadence never does

* Reach a model except through LoadCoach's HTTP API (ADR-0045) — `modelrack` is not a
  dependency, asserted by import-linter.
* Log a prompt, a response, a bearer token or the LoadCoach API key; `logging.include_content =
  true` is the one exception for transcript text at DEBUG, and it is config-only.
* Treat a remote tier with no price record as free, or send confidential data to one.
* Let a model decide control flow: a step completes only on a declared finish or a
  schema-validated result, and a refused tool call never ends a trajectory.

## The checklist

Security Standards §14 is held item by item in `tests/security/`; run it with
`pytest tests/security`. `promptcadence doctor` reports the exposure decision it finds. To report
a vulnerability, see [`SECURITY.md`](../SECURITY.md).
