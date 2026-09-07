# Contributing to PromptCadence

This repository is one component of the Local AI Suite. Before changing anything, read
`docs/apps/promptcadence/spec.md`, `docs/apps/promptcadence/lifecycle.md` and the current
phase in `development-plan.md` — all three are in this repository's `docs/` folder, copied from the
suite's central documentation set so this repository can be worked on independently.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

## Required reading, in order

1. This component's spec — purpose, scope, non-goals, contracts.
2. `lifecycle.md` in the same folder — the trajectory state machine, the plan/approval/
   `ExecutionIntent` chain, deviation handling and the explanation.
3. `development-plan.md` in the same folder — the phase you are implementing, its acceptance
   criteria and its tests.

## Rules that apply to every change here

* Follow the architecture's dependency direction.
  This repository's `.importlinter` enforces it in CI; do not weaken that file to make an import work.
* No business logic in a route handler or CLI command body — both call one service method and render.
* **The configurable bypass removes the up-front planning-and-approval round trip; it never removes
  per-turn governance.** A bypassed trajectory still resolves a tier, still checks budget, still
  checks data classification, still writes the same audit trail — every change to the loop must
  keep that distinction, and the planned-vs-bypassed record-diff test is what proves it still holds.
* An unavailable measurement is `Unsupported`, never zero, never `None` used as a substitute.
* Prompts are versioned JSON records, not Python string literals.
* No provider access of PromptCadence's own — every generation goes through LoadCoach's HTTP API;
  this repository never imports ModelRack and never talks to a provider directly.
* Every phase's acceptance criteria in `development-plan.md` must be demonstrable, not merely
  test-covered — the plan states what to run and what a person should see.

## Before opening a pull request

```bash
ruff format --check .
ruff check .
mypy src tests
lint-imports
pytest -m "not live and not performance"
```

All of the above run in CI (`.github/workflows/ci.yml`); a red CI run blocks merge.

## Commit style

Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `perf:`, `build:`,
`ci:`), with `!` or a `BREAKING CHANGE:` footer for breaking changes. Update `CHANGELOG.md` under
`## [Unreleased]` for any user-visible change.
