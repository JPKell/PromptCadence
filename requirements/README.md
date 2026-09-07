# Lockfiles

Exact, hash-verified pins for this repository's **own** CI and release pipeline, required by
Packaging and Release Standards §4 and Security Standards §11.

| File | Contents | Used by |
|---|---|---|
| `release.in` / `release.lock` | The build and publish chain (`build`, `hatchling`, `twine`) | `release.yml`, and CI's `build` job |
| `ci.lock` | Runtime dependencies plus the `dev` and `postgres` extras | Every CI job that installs this package |

## What these are not

They do **not** define what a consumer installs. `pip install promptcadence` resolves the
compatible ranges in `pyproject.toml`; an application that shipped pinned runtime dependencies
would be un-coinstallable with the rest of the suite. These files exist so that a green build stays
green: without them every CI run re-resolves, and a new `ruff` or `mypy` release can change the
result with no commit to explain it — and `pip-audit` would be auditing today's resolution rather
than what the build actually used.

## `ci.lock`

`requirements/ci.lock` pins the runtime dependencies plus the `dev` and `postgres` extras, with
hashes, resolved against PyPI on 2026-09-06 (row I2, the 1.0 release). Every suite package it
names — `baseaicore`, `setspec`, `weightsdb`, `mirrorwall`, `toolyard`, `cutctx`, `loadledger`,
`commissioner` — is a published release; a lock whose hashes name artifacts no index serves
installs nowhere. Every CI job that installs this package runs

```yaml
      - run: pip install --require-hashes -r requirements/ci.lock
      - run: pip install . --no-deps
```

except the 3.14 early-warning job, which resolves from ranges on purpose. The one trap is the
second line: without `--no-deps`, `pip install .` re-resolves the ranges and the lock stops
meaning anything.

Regenerate after any change to `pyproject.toml`'s dependencies or extras:

```bash
pip install "pip-tools==7.6.1"
pip-compile --extra=dev --extra=postgres --generate-hashes --no-emit-index-url \
    --output-file=requirements/ci.lock --pip-args='--no-cache-dir' --strip-extras pyproject.toml
```

Locally the repository still runs against editable installs; the lock is what makes a green CI
build mean something. The clean-venv proof for 1.0 — install from the lock, install the package
`--no-deps`, run the suite — is recorded in `docs/history/I2_HANDOFF.md`.

## Coverage measures the installed package, not the checkout

`[tool.coverage.run] source` in `pyproject.toml` names the **importable package** (`promptcadence`)
rather than `src/promptcadence`, with a `[tool.coverage.paths]` mapping back to the checkout. A
path-based source reports 0 % against the non-editable install CI uses — the tests all pass,
nothing is measured, and the coverage gate fails with a number that looks like a catastrophe
instead of a configuration error.

## `release.lock`

Run after any change to `release.in`, and commit the result:

```bash
pip install pip-tools
pip-compile --strip-extras --generate-hashes \
    --output-file requirements/release.lock requirements/release.in
```

`uv pip compile` is the sanctioned alternative (Security Standards §11). `release.in` is identical
across the suite, and so is the lock it resolves to: this file is byte-identical to LoadCoach's
and LoadLedger's (generated with **pip-tools 7.6.1** on Python 3.13). That identity is the check
that the chain is reproducible rather than merely pinned.

## Interpreter

Both locks were resolved on Python 3.13. Every pin's `requires-python` admits 3.12, and no pin is
CPython-ABI-specific, so the same lock installs under the release workflow's 3.12 and under the
3.13 the reference machine runs.
