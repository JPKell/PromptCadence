# Memory safety — keeping a model server from taking the machine down

**Audience:** an operator running Ollama or `llama-server` behind FreeWeight, LoadCoach and, through
LoadCoach, IdeaPress and PromptCadence. **Reference machine:** one RTX 5060 Ti (16 GB VRAM),
30 GB RAM, 8 GB swap, Ubuntu with `systemd-oomd`. Sizes below are for that machine; scale to yours.

**Read first:** [ADR-0119](adr/0119-model-servers-run-under-a-host-memory-cap.md) (the host-memory
cap), [ADR-0120](adr/0120-kv-cache-precision-and-flash-attention-are-per-model-llamacpp-settings.md)
(KV-cache precision and flash attention per model),
[ADR-0121](adr/0121-freeweight-launches-llama-server-with-fit-off-and-caps-the-max-fit-ladder.md)
(`--fit off` and the max-fit ceiling), [ADR-0038](adr/0038-one-model-at-a-time-per-gpu.md) (why one
resident model), [ADR-0023](adr/0023-runtime-profile-resolution.md) (the runtime profile).

---

## 1. What happened, and why the machine hung rather than failing

On 2026-09-09 a FreeWeight run against Ollama made the reference machine unresponsive; it had to
be reset. The kernel log for the preceding fourteen days holds **no `oom-kill` line**: nothing was
killed. The machine did not run out of memory and recover — it ran out of memory and *thrashed*,
swapping until it could not be reached. Three facts line up:

1. **The Ollama daemon's default context was 112 000 tokens.**
   `/etc/systemd/system/ollama.service.d/override.conf` set `OLLAMA_CONTEXT_LENGTH=112000`. Any
   request that does not set `num_ctx` gets that context, and FreeWeight's `runtime.context_size`
   is unset by default ("the provider decides", recorded as `assumed`). A 112 k f16 KV cache on a
   12 GB model is far more than 16 GB of VRAM.
2. **Ollama never refuses; it spills.** Ollama's scheduler offloads what fits to the GPU and puts
   the rest — layers and KV cache — in host RAM. A 30 GB machine with the desktop, the test
   process and the page cache already resident has perhaps 20 GB to give. Past that the kernel
   swaps, and an 8 GB swap file on a model that is being *read* continuously is a thrash, not a
   failure.
3. **Nothing was watching the daemon.** `systemd-oomd` on Ubuntu monitors memory pressure only
   under `user@1000.service` (its shipped default: `ManagedOOMMemoryPressure=kill` at 50 %).
   `ollama.service` lives in `system.slice`. The pressure it caused was measured against the
   desktop session, whose processes were not the ones to kill.

The same shape is reachable through `llama-server`: its `--fit on` default (see §5) shrinks the
GPU layer count rather than failing, so a context that does not fit VRAM lands in host RAM the
same way. And `native.memory_kv`'s `max_context_fit` test climbs 8 k → 16 k → 32 k → 64 k → 131 k
**by design, expecting a refusal** — against a provider that spills instead of refusing, the test
itself is the reproduction.

**The principle every fix below applies:** a model server that cannot fit must **fail fast and
loud** — a launch error, a killed process, a recorded failed sample — never degrade into the host's
memory. Failure is a measurement (`memory_kv` catalog §3.2); thrash is a lost machine.

---

## 2. Host protections — do these first, no code involved

### 2.1 Ollama: cap the daemon, fix its default context, one resident model

Edit `/etc/systemd/system/ollama.service.d/override.conf` to read exactly:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
# The daemon default for requests that set no num_ctx. The applications set it per run;
# this is the floor for ad-hoc use. Never 112000 again on a 16 GB card.
Environment="OLLAMA_CONTEXT_LENGTH=8192"
# ADR-0038: one model per GPU. 0 (Ollama's default) means "up to 3 per GPU".
Environment="OLLAMA_MAX_LOADED_MODELS=1"
# cgroup memory cap (ADR-0119). MemoryHigh throttles, MemoryMax kills. Swap is denied to the
# daemon so a kill is fast rather than a thrash. Restart=always in the base unit brings it back.
MemoryHigh=22G
MemoryMax=24G
MemorySwapMax=0
# Put systemd-oomd on this unit as well as on the desktop session.
ManagedOOMMemoryPressure=kill
ManagedOOMMemoryPressureLimit=50%
```

Then:

```bash
sudo systemctl daemon-reload && sudo systemctl restart ollama
systemctl show ollama -p MemoryMax -p MemorySwapMax -p ManagedOOMMemoryPressure   # confirm
oomctl | grep -A2 ollama                                                           # monitored
```

What each line buys:

| Line | Protects against |
|---|---|
| `OLLAMA_CONTEXT_LENGTH=8192` | The 112 k default KV cache on every unset-context request. |
| `OLLAMA_MAX_LOADED_MODELS=1` | A second model loading beside the first (a jury juror, a chat in another window) and the pair spilling. |
| `MemoryHigh=22G` | The kernel throttles the daemon's allocations at 22 GB, before the cap. |
| `MemoryMax=24G` | The kernel OOM-kills the daemon's runner at 24 GB. The desktop keeps ≥ 6 GB. |
| `MemorySwapMax=0` | The daemon cannot swap, so hitting the cap is a kill in milliseconds, not minutes of thrash. |
| `ManagedOOMMemoryPressure=kill` | `systemd-oomd` kills the unit at sustained 50 % memory pressure even below the cap. |

Numbers are for 30 GB RAM. Rule of thumb: `MemoryMax` = RAM − 6 GB, `MemoryHigh` = `MemoryMax`
− 2 GB. On a 64 GB machine use 56 G / 54 G.

**Do not** set `OLLAMA_FLASH_ATTENTION` or `OLLAMA_KV_CACHE_TYPE` in this file to save memory
unless you accept that **every Ollama measurement becomes a different subject** — those two are
daemon-wide, not per request (ADR-0120 §"Ollama"), and FreeWeight cannot record what you set.
KV-cache quantization is a llama.cpp feature in this suite.

### 2.2 `llama-server` under LoadCoach or FreeWeight: the unit carries the cap; a shell run wraps itself

`llama-server` is a child of the application that launched it (ADR-0062) and inherits its cgroup.
Two layers now cover the parent:

* **Unit-managed applications (the operator path since row W0).** WeightRoom writes the
  `systemd --user` units for the four applications, and the `freeweight` and `loadcoach` units
  carry the cap themselves ([ADR-0125](adr/0125-weightroom-drives-the-applications-through-systemd-user-units-it-writes.md)
  rule 1; values from WeightRoom's `[host] memory_high` / `memory_max`, defaulting to
  RAM − 8 GB / RAM − 6 GB):

  ```ini
  [Service]
  MemoryHigh=22G
  MemoryMax=24G
  MemorySwapMax=0
  ```

  `weightroom units sync` writes them; `weightroom doctor` reports a unit missing them. Until
  WeightRoom's row W2 ships, write the same three lines by hand into
  `~/.config/systemd/user/<app>.service` (`systemctl --user daemon-reload && systemctl --user
  restart <app>`), or use the wrapper below.
* **Ad-hoc runs from a shell** — a one-off `freeweight run start`, `pytest -m live`, a developer
  `loadcoach serve` in a terminal — wrap the parent, exactly as before:

  ```bash
  systemd-run --user --scope -p MemoryHigh=22G -p MemoryMax=24G -p MemorySwapMax=0 \
      freeweight run start --suite native.memory_kv --model llamacpp/…
  systemd-run --user --scope -p MemoryHigh=22G -p MemoryMax=24G -p MemorySwapMax=0 \
      pytest -m live
  ```

Both land under `user@1000.service`, which `systemd-oomd` already watches, so the cap and the
pressure kill both apply. When the cap fires the kernel kills the largest process in the cgroup —
`llama-server` — and the application's supervisor records a dead server, not a hung machine. The
launcher-level cap of §3.1 (`provider.memory_max_bytes`, shipped at row N4) sits inside either as
the inner belt on `llama-server` itself; the unit or scope is the outer belt on the parent and
anything else it spawns.

### 2.3 Verify the guard before trusting it

```bash
# Drive Ollama past the cap on purpose. Expect: the runner dies, `ollama ps` empties, the
# desktop stays responsive, journalctl -u ollama shows the kill, the daemon restarts in 3 s.
curl -s http://127.0.0.1:11434/api/generate -d \
  '{"model":"gemma4:12b-it-q8_0","prompt":"hi","options":{"num_ctx":131072}}'
journalctl -u ollama -k --since "-2m" | grep -iE 'oom|killed|memory'
```

A guard that has not been fired once is a hope. Fire it.

---

## 3. What the applications change

Each item is one roadmap row (`roadmap/outstanding-work.md` N4–N6). Nothing below is built at the
time of writing; the host protections in §2 stand alone and are sufficient on their own.

### 3.1 ModelRack — `llama-server` is launched under a memory cap (ADR-0119, row N4)

`LlamaCppProvider` gains two keyword-only constructor settings, both optional:

| Setting | Type | Default | Effect |
|---|---|---|---|
| `memory_max_bytes` | `int \| None` | `None` | Wrap the launch in `systemd-run --user --scope -p MemoryMax=<n> -p MemorySwapMax=0`. |
| `memory_high_bytes` | `int \| None` | `None` | Adds `-p MemoryHigh=<n>`; requires `memory_max_bytes`. |

The wrapper prefixes `LaunchSpec.argv`; nothing else about supervision changes — the child is
still a session leader, its stderr still goes to the state directory, and the orphan sweep still
finds it (the scope name carries the pid). `systemd-run` missing on `PATH` while a cap is set is a
launch error naming both, never a silent uncapped launch. The applications expose them as
`provider.memory_max_bytes` / `provider.memory_high_bytes` (LoadCoach registration and FreeWeight
`[provider]`), unit in the name as always.

**Not in scope:** capping *Ollama* from the application. Ollama is a system service the
application connects to; its cap is the unit file in §2.1.

### 3.2 FreeWeight 1.2 — three runtime settings and one ceiling (ADR-0120, ADR-0121, row N5)

**`[runtime]` gains `flash_attention` and `kv_cache_precision`.** Both are already fields of
`baseaicore.RuntimeProfile` and already columns of `runtime_profiles` hashed into
`profile_hash` (data-model §runtime_profiles) — FreeWeight simply never let a run set them. They
are honoured on `provider.kind = "llamacpp"` only:

```toml
[provider]
kind = "llamacpp"
model_directory = "~/ai/models/llm"
memory_max_bytes = 25769803776         # 24 GiB (row N4)

[runtime]
context_size = 8192                    # never leave this unset again — see §1 fact 1
flash_attention = true
kv_cache_precision = "q8_0"            # "f16" (default), "q8_0" or "q4_0"
```

Rules, all validation errors that name the key:

* `kv_cache_precision` is one of `f16`, `q8_0`, `q4_0`. Anything else is refused.
* A quantized KV cache (`q8_0`, `q4_0`) **requires `flash_attention = true`** — llama.cpp cannot
  quantize the V cache without it and silently keeps f16 otherwise (ADR-0120 rule 3).
* Either key set with `provider.kind = "ollama"` is refused at startup: `runtime.flash_attention
  is not honoured by provider kind "ollama" (daemon-wide OLLAMA_FLASH_ATTENTION); unset it or use
  kind = "llamacpp"`. Refused, not ignored, because a profile that records a setting the server
  did not run is a fabricated subject (ADR-0007 rule 2).

Per-model values come from the runtime profile chain exactly as `context_size` does today
(ADR-0023): `[runtime]` is the default; a run's `--runtime` override or the API's `runtime` object
sets one model's values. **A different KV precision is a different `profile_hash`, therefore a
different measurement subject** (ADR-0017): a model measured at f16 and the same model at q8_0
sit in two columns of the comparison and are never averaged.

**`[runtime]` also carries `fit_to_device`, default `false`** — `--fit off` on every
`llama-server` FreeWeight launches (ADR-0121 §2). It travels as `provider_options["--fit"]`, so it
is in the hash and every profile stored before this version keeps its hash (those runs launched
with llama-server's default, `on`, and the key's absence still means that).

**`[benchmarks]` gains `max_fit_context_tokens`**, default `131072`, the ceiling of
`memory_kv.max_context_fit`'s ladder `8192, 16384, 32768, 65536, 131072` — truncated to the
ceiling the same way `long_context_max_tokens` truncates `native.long_context`'s ladder, hashed
into `dataset_hashes` the same way (ADR-0121 §1). On the reference machine set it to `32768` for
Ollama, leave it at the default for llama.cpp with `--fit off` and a memory cap: the 65 k rung then
fails at launch in seconds and is recorded as the OOM measurement the catalog asks for.

### 3.3 LoadCoach 1.3 — per-model KV precision and flash attention (ADR-0120, row N6)

`[runtime]` already carries `kv_cache_precision` and `flash_attention` as defaults. The per-model
override table gains the same two keys (today it has `context_size` only):

```toml
[runtime]
kv_cache_precision = "q8_0"
flash_attention = true

[runtime.models."llamacpp/gemma-4-12b-it.q4_k_m@sha256:…"]
context_size = 32768
kv_cache_precision = "q4_0"           # this one model, tighter

[runtime.models."ollama/qwen3.5:9b-q8_0@sha256:…"]
kv_cache_precision = ""               # refused — see below
```

The same three rules as §3.2 apply, evaluated where the profile is resolved
(`domain/routing/subject.py`): a quantized cache requires flash attention; a value other than the
three named is refused; and **either key resolved against an Ollama registration is refused by
name at resolve time**, which surfaces in `route explain` as a rejection with the reason as data
rather than as a served request whose recorded profile is false. `loadcoach models show` renders
the resolved precision and flash-attention state per model.

LoadCoach keeps llama-server's `--fit on` (ADR-0121 §3): for an agent loop, a model that serves
slowly beats one that does not serve. The memory cap is what protects the host there; the served
layer split is a runtime fact LoadCoach does not record.

### 3.4 Why Ollama is left alone

Ollama 0.32 exposes no per-request or per-model flash-attention or KV-cache-type option — its
`Options` carry `num_ctx`, `num_gpu`, `use_mmap` and the sampling keys; the two memory knobs are
`OLLAMA_FLASH_ATTENTION` and `OLLAMA_KV_CACHE_TYPE`, read once by the daemon. The suite does not
invent an option it cannot honour (ADR-0007 rule 2), does not run a second daemon per cache mode,
and does not ask the operator to declare the daemon's state so it can be hashed as `assumed`.
**llama.cpp is the recommended provider when KV-cache precision matters** (decision 2026-09-09);
Ollama stays the zero-configuration default at f16.

---

## 4. Which context sizes fit — a reference table for the 16 GB card

KV cache per token ≈ `2 × layers × kv_heads × head_dim × bytes` (f16: 2 bytes; q8_0: ≈ 1.06;
q4_0: ≈ 0.56). Weights are the model file size. Leave ≥ 1 GB for compute buffers.

| Model (file) | Weights | f16 KV at 8 k | f16 at 32 k | q8_0 at 32 k | Fits 16 GB at |
|---|---|---|---|---|---|
| gemma-4 12b q8_0 (12 GB) | 12.0 GB | 0.9 GB | 3.6 GB | 1.9 GB | 8 k f16 · 16 k q8_0 · not 32 k |
| qwen3.5 9b q8_0 (10 GB) | 10.0 GB | 0.6 GB | 2.3 GB | 1.2 GB | 32 k f16 (tight) · 64 k q8_0 |
| gpt-oss 20b (13 GB) | 13.0 GB | 0.5 GB | 2.0 GB | 1.1 GB | 16 k f16 · 32 k q8_0 |
| qwen3.5 9b q4_k_m (5.6 GB) | 5.6 GB | 0.6 GB | 2.3 GB | 1.2 GB | 128 k f16 · 256 k q8_0 |

Numbers are estimates from the architecture — `freeweight results` for `native.memory_kv`
reports the measured `observed_mb_per_1k_context` and `max_successful_context_tokens`, which are
the figures to trust. The table exists so that a `[runtime]` block is written with the card in
mind rather than discovered by the kernel.

---

## 5. `--fit` — what llama-server does when it does not fit

`llama-server --fit on` (its default) **adjusts only arguments the caller left unset** so the
model plus KV cache fits VRAM minus 1024 MiB. The suite always passes `--ctx-size` when the
profile sets it, so `fit` never shrinks a set context; it shrinks `--n-gpu-layers`, which the
suite leaves unset — layers move to host RAM, the server starts, decode runs 5–20× slower and the
host fills. With context also unset, `fit` cuts it down to `--fit-ctx` (default 4096). ModelRack
reads the served `n_ctx` from `/props` and records it, so a fitted run is *honest* about its
context — but not about its layer split, which is not reported and not hashed.

`--fit off` adjusts nothing: the CUDA build offloads every layer, and no room means a CUDA
allocation failure at launch — `llama-server` exits, ModelRack raises a typed launch error naming
the model and argv, and nothing spills. The cost: a model that would have run slowly does not run
at all unless `runtime.gpu_layers` is set by hand.

The suite's split (ADR-0121): **FreeWeight launches with `--fit off`** — a measurement whose launch
differs from its recorded profile is not a measurement, and `max_context_fit` needs the refusal
to exist. **LoadCoach keeps `--fit on`** — serving slowly beats not serving for an agent, and the
memory cap of §2–§3.1 is what keeps the host safe there. Either can be overridden per profile
through `provider_options = { "--fit" = "on" }` / `"off"`.

---

## 6. Checklist

**Today, on the host (§2)** — `docs/scripts/apply_memory_safety.sh` does all four (`--fire` runs
§2.3); `MEMORY_MAX_G`, `MEMORY_HIGH_G`, `CONTEXT_TOKENS` in the environment change the sizes.
`weightroom doctor` (row W4) checks every line of §2.1 and §2.2 and prints this script's
invocation for whatever is missing; it never runs it, because §2.1 needs root:

- [ ] `override.conf` rewritten as in §2.1; `daemon-reload`; `restart ollama`; `oomctl` shows the unit
- [ ] `~/.config/freeweight/config.toml` has `runtime.context_size = 8192` (or your chosen size) —
      never unset
- [ ] The `freeweight` and `loadcoach` units carry the three `Memory*` lines of §2.2 (WeightRoom
      writes them; by hand until row W2), and shell runs use the `systemd-run --user --scope` wrapper
- [ ] The guard fired once on purpose (§2.3) and the desktop survived

**Rows (§3):** N4 ModelRack launcher cap → N5 FreeWeight 1.2 (settings, ceiling, `--fit off`) →
N6 LoadCoach 1.3 (per-model override). N4 first; N5 and N6 are independent of each other.

**After N4/N5/N6:** keep the unit-level cap of §2.2 as the outer belt and rely on
`provider.memory_max_bytes` as the inner one; set
`benchmarks.max_fit_context_tokens` for Ollama installations; choose per model whether a
quantized KV cache is a subject you want measured.
