# Tiers and task profiles

A **tier** is PromptCadence's name for an execution surface: exactly one LoadCoach task profile,
an egress class (`remote = true|false`), a data-classification ceiling, and a context budget.
PromptCadence does no routing of its own — *which model* serves a tier is LoadCoach's decision,
driven by the task profile (ADR-0047). The normative text is
[the spec's §12 and lifecycle §3](apps/promptcadence/spec.md); this is the operator's view.

## The shipped defaults

Two local tiers are active out of the box, and no remote one:

```toml
[tiers.local_fast]
task_profile = "tools.agent.local_fast"
remote = false                        # local ⇒ may see confidential data
context_budget_tokens = 16384

[tiers.local_large]
task_profile = "tools.agent.local_large"
remote = false
context_budget_tokens = 32768

[policy]
default_tier = "local_fast"           # where a bypass or unplanned turn starts
escalation_order = ["local_fast", "local_large"]
```

`promptcadence config init` writes the full example, with `remote_cheap` and `remote_frontier`
commented out: a remote tier without a price list refuses to start, so shipping them active would
break "starts with zero configuration" (spec §20 AC1).

**Setting any `[tiers.<name>]` key replaces the whole shipped map** rather than extending it — a
`config.toml` or an environment that names one tier must name every tier it wants.

## Classification and egress

Every trajectory declares a data classification (`confidential` by default — the safe reading of
unclassified data), and every remote tier declares the highest classification it may see. A step
whose trajectory exceeds the tier's ceiling is refused **before any request is built**, and the
refusal is a recorded `EgressDecision` (spec §20 #4). The classification comes from the
trajectory's declaration, never from model text — a plan or an answer asking for a remote tier
changes nothing. Local tiers are approved with `target_not_remote` on every turn, so "every turn
carries an egress decision" is checkable by counting.

The pre-flight order on every turn is fixed (ADR-0073): **egress, then pricing, then
availability, then budget**, all before `turn.started`.

## What a remote tier needs

A remote tier is *configurable* from Phase 6 and *available* when two things are true, checked in
this order and reported by `promptcadence tiers check`, `doctor`, `GET /api/v1/tiers` and the
console's Tiers page (ADR-0098):

| Reason a remote tier cannot serve | What it means | What fixes it |
|---|---|---|
| `loadcoach_has_no_remote_provider` | LoadCoach has no `[providers.<name>]` registration declaring `remote = true` | Register one in LoadCoach 1.1 (its `providers` blocks). PromptCadence reads the fact from LoadCoach's `/models` listing when it renders `is_remote`; **LoadCoach 1.1.0 renders it on the generate response only**, so on that version the fact stays `false` until LoadCoach adds it to the listing — remote tiers refuse honestly meanwhile |
| `unpriced` | The tier's `pricing_file` holds no `ModelPricing` record claiming the current instant | Add a record for the serving model (ADR-0072 — JSON, a `records` array, rates as decimal strings, an omitted rate meaning "not stated", never free) |

Unpriced egress is refused with `UNPRICED_EGRESS_REFUSED`, never treated as free (spec §11
contract 5). Money is never stored: every debit keeps the four token classes and the
`pricing_hash`, and cost is re-derived (ADR-0030).

```toml
[tiers.remote_cheap]
task_profile = "tools.agent.remote_cheap"
remote = true
max_data_classification = "internal"  # never confidential
context_budget_tokens = 128000
pricing_file = "/etc/promptcadence/pricing/remote_cheap.json"
```

```json
{
  "records": [
    {
      "provider_kind": "openai_compatible",
      "provider_model_name": "qwen3:8b",
      "source": "provider_published",
      "observed_at": "2026-09-01T00:00:00Z",
      "price_tier": "standard",
      "rates": {
        "currency": "USD",
        "input_per_million_tokens": "2.50",
        "output_per_million_tokens": "10.00"
      }
    }
  ]
}
```

## The live remote run (roadmap I13)

PromptCadence 1.0 ships with the recorded-transport half of I13 proven in CI — a `remote_cheap`
step served by a remote registration, the egress decision approved with a remote target, the
spend priced and debited, the explanation naming the provider, and a local tier never doing any
of that — and the live half deferred (ADR-0098). When you have an endpoint and a key:

```bash
# 1. In LoadCoach: a registration declaring itself remote, e.g. [providers.openrouter] with
#    kind = "openai_compatible", remote = true, and its credential. Restart LoadCoach.
# 2. Price the tier (above), point [tiers.remote_cheap] pricing_file at it, restart PromptCadence.
promptcadence tiers check                        # remote_cheap: available
promptcadence run "summarize the files in ./notes" --tier remote_cheap \
    --classification internal --follow
promptcadence trajectory explain <id>            # the turn's model.provider_name, the priced debit,
                                                 # the approved remote egress decision
```

A run against an unpriced tier proves spec §20 #5 (the `UNPRICED_EGRESS_REFUSED` refusal), which
is already proved; it does not prove I13. Price the tier first.

## Changing the answer

* **Pin a tier per trajectory** with `--tier` (`POST /trajectories` `tier`); policy still applies.
* **Escalation** is explicit and ordered: `[policy] escalation_order`, never a ranking. A tier
  that cannot serve falls to the intent's next permitted tier; when none can, the turn is a
  `tier_escalation` deviation whose scoped re-approval carries the next tier in the order.
* **Context budgets** drive compaction: above `threshold × context_budget_tokens` the transcript
  is compacted back to that figure, as a view over the rows, never a deletion; the summary turn
  runs on the cheapest admissible local tier under its own intent revision (ADR-0090).
* **Task profiles** are LoadCoach's to edit; `promptcadence tiers check` tells you when one is
  missing or disabled there.
