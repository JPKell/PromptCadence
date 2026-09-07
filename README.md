# PromptCadence

A plan-approved, tier-routed agent loop over LoadCoach in which every step is proposed in a plan,
the plan is approved against governance policy and remaining budget before any step executes, and
every turn that does execute is fully reconstructable afterwards — which model ran it, on which
tier, on what data, at what cost, under whose approval.

**Status:** `1.2.0`, on PyPI. Every phase of the development plan is built and gated: planning with
corrective retries and three approval modes, the `ExecutionIntent` every turn runs under, sandboxed
tools under ToolYard's isolation ladder, the budget over LoadLedger with three ceilings, egress
governance over Commissioner with a durable decision per turn, per-step retry, context compaction
as a view, the composed and materialized explanation, the operator console, the retention sweep,
the security checklist and the prompt-injection corpus as release gates, and every spec §15
budget asserted. Runtime settings (1.1) can change while the server runs, with a CLI verb (1.2) to
read and set them over HTTP. Remote tiers refuse honestly until LoadCoach has a registration
declaring `remote = true` and the tier is priced (ADR-0098). See [docs/](docs/README.md) for the
operator set and the [development plan](docs/apps/promptcadence/development-plan.md) for what each
phase added.

Part of the **Local AI Suite**. Reaches a model only through [LoadCoach](https://github.com/JPKell/LoadCoach)'s
HTTP API — it never imports a model provider directly ([ADR-0045](docs/adr/0045-promptcadence-reaches-models-only-through-loadcoach.md)
in the suite's shared documentation).

## Install

```bash
pip install promptcadence
promptcadence serve
```

Starts on `127.0.0.1:8768` with zero configuration. Health reports the `loadcoach` component
degraded (not unavailable) when no LoadCoach is reachable — PromptCadence requires LoadCoach for
*execution*, never for startup. See
[docs/apps/promptcadence/spec.md](docs/apps/promptcadence/spec.md) §12 for the full configuration
surface and `PROMPTCADENCE_*` environment variables.

## Quickstart

```bash
pip install promptcadence
promptcadence serve            # starts the API on 127.0.0.1:8768
promptcadence health --json    # same health data the API reports, from the CLI
promptcadence run "summarize the files in ./notes" --follow     # plan, approve, execute, explain
promptcadence run "summarize the files in ./notes" --bypass-planning --follow
promptcadence trajectory explain <id>                           # every model, tier, tool call, debit, egress verdict
promptcadence --help
```

Open <http://127.0.0.1:8768/> for the console. Read [docs/quickstart.md](docs/quickstart.md) next.

PromptCadence 1.2 is tested against LoadCoach `1.1.1` and needs `≥ 1.1`: the declared finish
reason on the wire (spec §11 contract 6 — an undeclared finish is never read as success), tool
definitions and `tool_calls` on `/generate`, and the serving registration's `is_remote` on every
response. See [docs/upgrading.md](docs/upgrading.md) for the compatibility table.

## Compatibility

Declared version ranges from `pyproject.toml` — kept from drifting by
`tests/unit/test_readme_compatibility.py`, which parses the file and fails if this table disagrees:

| Package | Range |
|---|---|
| `baseaicore` | `>=0.4.1,<0.5` |
| `setspec` | `>=0.5,<0.7` |
| `weightsdb` | `>=0.2,<0.3` |
| `mirrorwall` | `>=0.2.2,<0.3` |
| `toolyard` | `>=0.1.1,<0.2` |
| `cutctx` | `>=0.1,<0.2` |
| `loadledger` | `>=0.3,<0.4` |
| `commissioner` | `>=0.1,<0.2` |

`modelrack` and `sweatmeter` are deliberately absent: PromptCadence reaches a model only through
LoadCoach's HTTP API, and telemetry is displayed from LoadCoach's own `/system/status` (ADR-0045
rule 2, in the suite's central `docs/adr/`). `loadledger` and `commissioner` are installed with
their `[sql]` extra, which is not optional in practice: both tables are mounted unconditionally into
this application's own metadata and Alembic history.

## Documentation

| Read this | For |
|---|---|
| [docs/README.md](docs/README.md) | The operator set: quickstart, configuration reference, tiers, security, operations, troubleshooting, upgrading, the OpenAPI snapshot |
| [docs/apps/promptcadence/spec.md](docs/apps/promptcadence/spec.md) | Purpose, scope, non-goals, public contracts, configuration, acceptance criteria |
| [docs/apps/promptcadence/lifecycle.md](docs/apps/promptcadence/lifecycle.md) | The trajectory state machine, deviation categories and estimator |
| [docs/apps/promptcadence/development-plan.md](docs/apps/promptcadence/development-plan.md) | The phased build plan: goals, work, tests, acceptance criteria per phase |

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
pytest -m "not live and not performance"
```

See [`SECURITY.md`](SECURITY.md) for how to report a vulnerability.

## License

Apache-2.0 — see [`LICENSE`](LICENSE).
