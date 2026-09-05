# Lockfiles

Exact, hash-verified pins for this repository's **own** release pipeline, required by
Packaging and Release Standards §4 and Security Standards §11.

| File | Contents | Used by |
|---|---|---|
| `release.in` / `release.lock` | The build and publish chain (`build`, `hatchling`, `twine`) | `release.yml`: the tagged release and the manual TestPyPI dry run |

There is **no `ci.lock` yet**: CI still installs the `dev` extra from `pyproject.toml`'s ranges
(`pip install -e ".[dev]"`). Generating and adopting it is a separate change — the
`ci.lock`-installs-from-artifacts shape the other repositories use is recorded in LoadCoach's
`requirements/README.md`, and the trap it names (a path-based coverage `source` reporting 0 %
against a non-editable install) applies here too when it lands.

## What this is not

`release.lock` does **not** define what a consumer installs. `pip install promptcadence` resolves
the compatible ranges in `pyproject.toml`; an application that shipped pinned runtime dependencies
would be un-coinstallable with the rest of the suite. The lock exists so that the artifact the
TestPyPI dry run proved is the artifact the tagged release builds: both jobs run
`pip install --require-hashes -r requirements/release.lock` and then `python -m build
--no-isolation`, so `hatchling` comes from the lock rather than being re-resolved from PyPI at
build time.

## Regenerating

Run after any change to `release.in`, and commit the result:

```bash
pip install pip-tools
pip-compile --strip-extras --generate-hashes \
    --output-file requirements/release.lock requirements/release.in
```

`uv pip compile` is the sanctioned alternative (Security Standards §11).

`release.in` is identical across the suite, and so is the lock it resolves to: this file is
byte-identical to LoadCoach's and LoadLedger's (generated with **pip-tools 7.6.1** on Python 3.13,
whose header records `--no-index` — part of the recorded command, not of the resolution, which was
against PyPI). That identity is the check that the chain is reproducible rather than merely
pinned; it is how this copy was brought in, and re-running the command above reproduces it.

## Interpreter

Resolved on Python 3.13. Every pin's `requires-python` admits 3.12, and no pin is
CPython-ABI-specific, so the same lock installs under the release workflow's 3.12 and under the
3.13 the reference machine runs.
