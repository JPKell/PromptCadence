# PromptCadence

A plan-approved, tier-routed agent loop over LoadCoach in which every step is proposed in a plan,
the plan is approved against governance policy and remaining budget before any step executes, and
every turn that does execute is fully reconstructable afterwards — which model ran it, on which
tier, on what data, at what cost, under whose approval.

**Status:** Phase 3 (LoadCoach client, bypass loop, events and recovery), unreleased. A
bypassed trajectory executes end to end: `promptcadence run "…" --bypass-planning --follow`
queues it, the worker claims it under a lease, mints its `ExecutionIntent`, runs each turn through
LoadCoach's `/generate`, records the turn with its provenance and every deviation, streams the
events over SSE, and survives a `kill -9` mid-turn with no duplicated turn and no orphaned
LoadCoach job. Planning, tools, budget enforcement and egress policy arrive in the phases that
follow; a planned trajectory is claimed and failed with that cause rather than queued forever. See
the [development plan](docs/apps/promptcadence/development-plan.md) for what each phase adds.

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
promptcadence run "summarize the files in ./notes" --bypass-planning --follow
promptcadence trajectory list
promptcadence --help
```

A note on LoadCoach versions: PromptCadence never reads an undeclared finish as success (spec
§11 contract 6), and it reads the provider's declared reason from `output.finish_reason`, which
LoadCoach renders since its commit `846348b`. Against an older LoadCoach (`1.0.0`,
`01170a7`), which recorded the reason but rendered it nowhere, a free-text tier halts on its
first turn with that cause on the row, and only a tier whose task profile validates a JSON
Schema completes.

## Documentation

| Read this | For |
|---|---|
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
