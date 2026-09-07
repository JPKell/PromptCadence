# Quickstart

PromptCadence runs a plan-approved, tier-routed agent loop over LoadCoach: every step is
proposed, approved against policy and budget before it executes, executed under an
`ExecutionIntent`, and reconstructable afterwards. It needs a running LoadCoach with a model
provider behind it, and nothing else — no cloud provider, no configuration beyond defaults.

## Install and start

```bash
pip install promptcadence
promptcadence serve                  # API and console on http://127.0.0.1:8768
```

`promptcadence serve` migrates its SQLite database under `$XDG_DATA_HOME/promptcadence/` on first
start and starts its worker. LoadCoach is expected at `http://127.0.0.1:8766` (`[loadcoach]
base_url`); when it is not there, health reports the `loadcoach` component **degraded**, never
unavailable — PromptCadence needs LoadCoach for execution, not for startup.

Check the installation before anything else:

```bash
promptcadence doctor                 # database, loadcoach, tiers, tools — ✓ / ! / ✗ with a reason
promptcadence tiers check            # every configured tier's task profile exists in LoadCoach
promptcadence tools list             # the tool registry, and the isolation rung run_command has
```

`tiers check` exits 4 when a tier's task profile is missing in LoadCoach or a remote tier cannot
serve; the five harness profiles (`tools.agent.local_fast`, `tools.agent.local_large`,
`tools.agent.remote_cheap`, `tools.agent.remote_frontier`, `tools.plan`) ship with LoadCoach 1.1.

## The first trajectory

```bash
promptcadence run "summarize the files in ./notes" --follow
```

That plans (`tools.plan`), approves the plan automatically under the shipped `auto` policy,
executes each step on `local_fast` with sandboxed tools, streams the events until the trajectory
ends, and exits 0 on `completed`, 5 on `halted`/`failed`/`rejected`, 6 on `cancelled`. The
trajectory's workspace is `<data>/workspaces/<trajectory_id>`; `./notes` is read through the
tools' read roots only if `[tools] read_roots` names it — by default a trajectory sees its own
workspace and nothing else.

The same task without planning, governance intact:

```bash
promptcadence run "summarize the files in ./notes" --bypass-planning --follow
```

## Reading what happened

```bash
promptcadence trajectory show <id>          # state, cause, lease, budget
promptcadence trajectory explain <id>       # the composed record: every model, tier, tool call,
                                            # debit, egress verdict, deviation and approval
promptcadence ledger show --scope day       # today's spend against the per-day ceiling
promptcadence egress list --denied-only     # every refused egress, as a queryable decision
```

Or open **http://127.0.0.1:8768/** — the console: dashboard, trajectories and their timelines,
the approvals inbox, tiers, tools, ledger, egress, system. It authenticates exactly as the API
does and adds no session cookie, so it is loopback-first by decision
([docs/security.md](security.md)).

## Approvals

With `[approval] mode = "hybrid"`, a step whose egress reaches `gate_egress_at` (`internal` by
default) or whose estimated cost passes `gate_step_cost` pauses the trajectory for a person;
`manual` pauses every plan. Then:

```bash
promptcadence approvals list
promptcadence approve <trajectory_id>            # or deny <id> --reason "…"
```

`approve` is its own scope, deliberately distinct from `write`: the identity that submits work
cannot approve its own egress ([docs/security.md](security.md)).

## Next

* [tiers.md](tiers.md) — tiers, task profiles, classifications, and what a remote tier needs.
* [configuration.md](configuration.md) — every key, generated from the settings model.
* [security.md](security.md) — before exposing PromptCadence beyond this machine.
* [operations.md](operations.md) — retention, backups, the worker, what to watch.
* [troubleshooting.md](troubleshooting.md) — every error code and what to do.
