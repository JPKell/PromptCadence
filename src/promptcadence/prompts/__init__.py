"""promptcadence.prompts — this application's own prompt records (ADR-0012, spec §9).

Three records, and no prompt text anywhere else in the package:

* ``planner.draft`` — what the planner asks LoadCoach for under ``tools.plan``.
* ``planner.corrective`` — the structured-output corrective: every validation issue, fed back at
  once, so a two-attempt budget is spent on correction rather than on discovering the next problem.
* ``step.execute`` — the framing turn a planned step's thread opens with, naming the step and the
  results of the steps it depends on.

``manifest.json`` pins every record's hash; ``load_pack`` refuses a stale manifest at startup and
``tests/unit/test_prompt_pack.py`` refuses it in CI. To regenerate after editing a record::

    python -c "from pathlib import Path; from setspec.prompts import build_manifest, \\
    write_manifest; m, _ = build_manifest(Path('src/promptcadence/prompts'), \\
    generated_at='2026-09-04T00:00:00Z'); write_manifest(m, Path('src/promptcadence/prompts'))"
"""
