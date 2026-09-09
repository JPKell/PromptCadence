# PromptCadence documentation

Operator documents, written for this release:

* [quickstart.md](quickstart.md) — install, the first trajectory, reading what happened, approvals.
* [configuration.md](configuration.md) — every key, generated from the settings model.
* [tiers.md](tiers.md) — tiers, task profiles, classifications, what a remote tier needs, the
  live remote run.
* [security.md](security.md) — the exposure path end to end, scopes, the model-output boundary,
  retention.
* [operations.md](operations.md) — health, the worker, retention, backups, budgets, what to watch.
* [troubleshooting.md](troubleshooting.md) — every error code and `doctor`'s four components.
* [upgrading.md](upgrading.md) — migrations, behaviour changes at 1.0, the downgrade path.
* [openapi.json](openapi.json) — the API, as a committed snapshot.
* [LLAMACPP_SETUP.md](LLAMACPP_SETUP.md) — running llama.cpp behind the suite: install, the model
  directory, adapters, residency and swapping, router mode, model suggestions (mirrored).
* [MEMORY_SAFETY.md](MEMORY_SAFETY.md) — keeping Ollama and `llama-server` from taking the host down: the cgroup caps, `--fit`, per-model KV-cache precision, the max-fit ceiling (mirrored).

The specification set — `apps/promptcadence/{spec,lifecycle,development-plan}.md` and the
standards and ADRs it cites — is **mirrored** from the suite's `docs/` repository, which is the
single source of truth. Edit there; this copy is downstream and byte-identical.
