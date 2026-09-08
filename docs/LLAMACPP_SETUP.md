# llama.cpp with the suite — install, models, adapters, swapping

**Audience:** an operator putting `llama-server` behind LoadCoach and FreeWeight (and, through
LoadCoach, IdeaPress and PromptCadence). **Reference machine:** one RTX 5060 Ti, 16 GB VRAM,
CUDA 13.1, Linux. Sizes below are for that card; scale the quantisation to yours.

**Read first:** [`architecture/adapter-identity-and-serving.md`](architecture/adapter-identity-and-serving.md)
(why llama.cpp, one base per process, hot-swap per request),
[ADR-0062](adr/0062-llamacpp-serves-adapters-through-a-supervised-process.md) (the supervised
process), [ADR-0071](adr/0071-modelrack-persists-artifact-digests-in-a-json-file-the-application-names.md)
(the digest cache), [ADR-0061](adr/0061-the-adapter-registry-is-a-directory-and-a-manifest.md)
(adapters are a directory and a manifest).

**The one idea that explains everything below:** the suite does not connect to a llama-server
you started. **The application launches and supervises `llama-server` itself** — one process
per base model, from a directory of GGUF files you name — and identifies every model by the
sha256 of its file, not by its filename. Ollama remains the zero-configuration default;
llama.cpp is the provider you choose when you want LoRA adapters, exact control of launch flags,
or models Ollama does not package.

---

## 1. Install `llama-server`

The suite needs one executable, `llama-server`, on `PATH` (or named by `provider.server_path`).
The tested build is tag **`b10792`** (ModelRack spec §18); newer builds usually work, and the
router-mode features in §6 need at least the late-2025 builds.

**Option A — prebuilt.** The [llama.cpp releases page](https://github.com/ggml-org/llama.cpp/releases)
ships archives per platform and backend. Unpack, put `llama-server` on `PATH`, check
`llama-server --version`. CPU and Vulkan archives are always there; check the page for a CUDA
archive matching your driver before building.

**Option B — build from source with CUDA** (what the reference machine runs):

```bash
git clone --depth 1 --branch b10792 https://github.com/ggml-org/llama.cpp ~/ai/tools/llama.cpp
cd ~/ai/tools/llama.cpp
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=120 -DLLAMA_CURL=OFF   # 120 = RTX 50xx
cmake --build build --target llama-server llama-bench llama-gguf-split -j
ln -sf ~/ai/tools/llama.cpp/build/bin/llama-server ~/.local/bin/llama-server
llama-server --version
```

`CMAKE_CUDA_ARCHITECTURES`: 120 for RTX 50xx, 89 for RTX 40xx, 86 for RTX 30xx. **CUDA 13.1 with
glibc ≥ 2.43** fails on `rsqrt` ("exception specification is incompatible", llama.cpp #19100).
The reference machine works around it with a user-local copy of the CUDA include tree in which
the four `rsqrt`/`rsqrtf` declarations are marked `noexcept(true)`, passed as
`CUDAFLAGS=-I<overlay> cmake … -DCMAKE_CUDA_FLAGS=-I<overlay>`. The system toolkit is never
patched. If your toolkit or glibc differs, you will not hit this.

**Check the GPU is used:** `llama-server -m <any>.gguf --port 8099 -ngl 99` and look for
`load_tensors: offloaded N/N layers to GPU` in the log. Stop it before starting any suite
application; §5 says why.

## 2. The model directory

One flat directory of single-file GGUFs. The reference layout:

```text
~/ai/models/llm/                      # bases — provider.model_directory
  Qwen2.5-1.5B-Instruct.Q8_0.gguf
  gemma-4-12B-it.Q4_K_M.gguf
~/ai/models/adapters/llm/             # LoRA adapters — [adapters] directory (LoadCoach, FreeWeight)
  qwen2.5-1.5b-instruct-terse.gguf
  qwen2.5-1.5b-instruct-terse.manifest.json   # drafted by `loadcoach adapters scan`, then reviewed
```

Rules the provider enforces (ModelRack spec §13):

* **Single-file GGUFs only.** A split model (`…-00001-of-00003.gguf`) is refused as `sharded`;
  merge it first: `llama-gguf-split --merge <first shard> <output>.gguf`.
* **Identity is the hash.** The canonical id is `llamacpp/<filename stem, lowercased>@sha256:<digest>`,
  e.g. `llamacpp/qwen2.5-1.5b-instruct.q8_0@sha256:5926a692b27b`. Renaming a file changes the
  name, not the identity; changing its bytes changes the identity, and every benchmark and
  routing record keyed on the old digest stays attached to the old bytes. That is the point.
* **Hashing is cached** in `<state_dir>/digests.json` (ADR-0071), keyed by path and file stamp.
  First discovery of a 40 GB directory takes about 45 s; afterwards it is free. Delete the file
  or run `… models refresh` if you suspect it; nothing can fail because of it.

## 3. Download a model

Use the Hugging Face CLI and download **one quantisation file** into the directory. Prefer
official `-GGUF` repositories (Qwen, ggml-org, Google, Mistral, unsloth, bartowski).

```bash
pip install -U "huggingface_hub[cli]"
hf download Qwen/Qwen2.5-Coder-7B-Instruct-GGUF \
   --include "*q8_0*.gguf" --local-dir ~/ai/models/llm
hf download ggml-org/gpt-oss-20b-GGUF --include "*.gguf" --local-dir ~/ai/models/llm
```

Then flatten anything the CLI nested (`find ~/ai/models/llm -name "*.gguf" -exec mv {} ~/ai/models/llm/ \;`)
and delete split shards you did not mean to keep. `llama-server -hf <repo>:<quant>` also works
for a quick trial, but it downloads into `~/.cache/llama.cpp`, which the suite does not read.

**Choosing a quantisation for 16 GB.** Weights plus KV cache plus about 1 GB of headroom must fit
(LoadCoach's `[telemetry] vram_headroom_bytes` is 512 MB by default and it will refuse a model
that does not fit as `insufficient_vram`, with the arithmetic in `route explain`). Rules of thumb:

| Parameters | Quantisation | Weights | Comfortable context on 16 GB |
|---|---|---:|---:|
| ≤ 4 B | Q8_0 | ≤ 4.5 GB | 32k+ |
| 7–9 B | Q8_0 | 8–10 GB | 16–32k |
| 12–14 B | Q4_K_M / Q5_K_M | 8–10 GB | 16k |
| 20 B MoE (gpt-oss) | MXFP4 | ≈ 12 GB | 16k |
| 24 B | Q4_K_M | ≈ 14 GB | 4–8k, tight |
| 30 B MoE (3 B active) | Q4_K_M, partial offload | ≈ 18 GB | usable; CPU takes the rest |

KV cache at f16 costs roughly `2 × layers × kv_heads × head_dim × 2 bytes` per token; LoadCoach
computes it exactly from the GGUF header and shows it in the routing explanation.
`runtime.kv_cache_precision = "q8_0"` halves it.

## 4. Point the suite at the directory

No registration step exists. A file in the directory is a model once discovered.

**LoadCoach** (`~/.config/loadcoach/config.toml`; the singular `[provider]` block or a named
registration — never both, ADR-0077):

```toml
[providers.llama]
kind = "llamacpp"
model_directory = "~/ai/models/llm"
remote = false
# state_dir defaults to <data_dir>/llamacpp/llama; server_path to "llama-server" on PATH

[adapters]
directory = "~/ai/models/adapters/llm"      # empty = adapters off

[runtime]
context_size = 16384                        # 0 = let llama-server decide from the GGUF
kv_cache_precision = "q8_0"                 # "" = llama-server's default (f16)
flash_attention = true

[runtime.models."llamacpp/gemma-4-12b-it.q4_k_m@sha256:<digest from models list>"]
context_size = 8192                         # per-model override, keyed by canonical id

[residency]
unload_idle_seconds = 900
max_resident_models = 1                     # per GPU; §5
```

```bash
loadcoach doctor                 # llama-server found? directory readable? GPU seen?
loadcoach models refresh         # hash and describe every GGUF (slow once)
loadcoach models list            # canonical ids, context, VRAM estimate, availability
loadcoach models show llamacpp/gemma-4-12b-it.q4_k_m@sha256:…
loadcoach serve
loadcoach route explain --task general.chat   # who would win, and why the others lost
```

The launch flags LoadCoach passes come from the runtime profile (ADR-0023): `--ctx-size`,
`--cache-type-k/v`, `--flash-attn`, then `--jinja --no-webui --host 127.0.0.1 --port <8180–8189>`,
plus `--lora <file> … --lora-init-without-apply` for every compatible adapter. GPU layer count is
left to llama-server, which on a CUDA build offloads everything that fits. **FreeWeight** adds
`runtime.gpu_layers`, `runtime.threads` and `runtime.batch_size` to that set.

**FreeWeight** (`~/.config/freeweight/config.toml`):

```toml
[provider]
kind = "llamacpp"
model_directory = "~/ai/models/llm"
[adapters]
directory = "~/ai/models/adapters/llm"
[runtime]
context_size = 8192                  # a different context is a different measurement subject
```

```bash
freeweight models refresh && freeweight models list
freeweight run start --suite native.echo --model llamacpp/qwen2.5-1.5b-instruct.q8_0@sha256:…
```

**IdeaPress and PromptCadence** never touch the directory. Set IdeaPress's backend to LoadCoach
(`[inference] mode = "loadcoach"`, LEARNING_PLAN §3.6) and PromptCadence always runs through
LoadCoach; routing picks the llama.cpp model by task profile. To pin a stage to one model, name
its canonical id in IdeaPress `[models.stages]` with `honour_stage_bindings = true`, which gives
up routing for that stage (ADR-0040) — usually not what you want.

**Adapters** (LEARNING_PLAN §1.6 and §2.5 walk it): drop the LoRA GGUF into the adapter
directory, run `loadcoach adapters scan` — it drafts a `model.adapter_manifest` beside the file,
hashing the artifact and reading `adapter_config.json` — then open the draft, fill
`declared_capabilities` and `data_classification` (required, no default), and keep it.
`loadcoach adapters list` shows registration status and base compatibility. A newly kept
manifest is `pending_restart` until the base's next idle unload; a PEFT/safetensors adapter is
converted once with `convert_lora_to_gguf.py` from the llama.cpp tree. Adapters are routable only
once FreeWeight has measured them (ADR-0064 rule 3, `require_adapter_evidence`); pins work
immediately.

## 5. Swapping, residency and "groups" — the suite's way

LoadCoach **is** the model swapper. What it does, and the knobs:

* **One `llama-server` per base, one base resident per GPU** (`[residency] max_resident_models`).
  A request whose winning candidate is not resident unloads the idle one (after
  `unload_idle_seconds`, or immediately when VRAM demands it), spawns the new server on the next
  free port in 8180–8189, waits on `/health`, and serves. Pid files and captured stderr live in
  `<data_dir>/llamacpp/<registration name>/`; `loadcoach models residency` shows what is up.
* **Adapters swap per request without a reload.** Every compatible adapter is pre-registered at
  launch and selected by the request's `lora` field; the base never moves. This is why the
  "group" you actually want — one base, several specialisations — costs nothing to switch.
* **Routing prefers what is resident, a little.** `prefer_resident_bonus = 0.05` and
  `base_switch_penalty = 0.10` are chosen, not measured — set the penalty from your own load
  times (`loadcoach serve`'s log records each load). A request may pass
  `ignore_residency = true` to say "use the best model and pay the swap"; it is recorded.
* **Task profiles are the grouping mechanism**, not model lists. `code.generate`, `general.chat`,
  `tools.agent.local_fast`, … each weight capabilities and set hard constraints
  (`min_context_tokens`, `allow_remote_providers`); every model that satisfies the constraints
  competes on evidence. To make a "coding group" you do not list models — you give the coding
  models coding evidence (FreeWeight) and let `code.*` profiles find them. Override the shipped
  file with `[routing] task_profiles_path`. PromptCadence's `[tiers.<name>]` map onto these.
* **Two GPUs:** raise `max_resident_models` and `[execution] max_concurrent_jobs`; LoadCoach
  reports multi-GPU placement as unknown rather than guessing (degradation matrix).
* **Never share the GPU with a FreeWeight benchmark.** ADR-0018's isolation ladder ends in
  refusal; a LoadCoach-resident model beside a running benchmark corrupts the measurement, and a
  benchmark beside LoadCoach makes every candidate `insufficient_vram`. Stop one before the other.
* **Ports 8180–8189 are LoadCoach's and FreeWeight's.** Run any manual `llama-server` elsewhere.
* **Leaked servers.** If `nvidia-smi` shows `llama-server` processes after every application is
  down, the supervisor's pid records in `<data_dir>/llamacpp/*/` name them; kill them by pid. This
  was a real defect (fixed at H3: LoadCoach now closes every provider on shutdown), so it should
  not recur — but check after a crash.
* **`GGML_CUDA_DISABLE_GRAPHS=1`** is set automatically on any launch that registers adapters:
  `b10792` leaks a CUDA graph per adapter switch. Your environment overrides it if you set it.

## 6. Swapping outside the suite — llama-server router mode and llama-swap

For tools that are not LoadCoach (an IDE plugin, a notebook, an embedding job), `llama-server`
has its own multi-model mode. Start it **with no `-m`** and point it at the same directory:

```bash
llama-server --models-dir ~/ai/models/llm --models-max 1 --port 8080 -c 16384 -ngl 99
curl localhost:8080/v1/models                     # every GGUF, loaded or not
curl localhost:8080/v1/chat/completions -d '{"model":"gemma-4-12B-it.Q4_K_M","messages":[…]}'
```

The `model` field picks the file; a model not loaded is loaded on first use, and `--models-max`
bounds how many stay up at once (default 4). Multimodal and split models go in a
subdirectory (`<name>/<name>.gguf` + `mmproj-*.gguf`). Per-model flags go in a preset INI:

```ini
; ~/ai/models/router.ini — llama-server --models-preset ~/ai/models/router.ini
version = 1
[*]                                   ; globals for every instance
c = 16384
ngl = 99
flash-attn = on
[gemma-4-12B-it.Q4_K_M]               ; a file in --models-dir: override its flags
c = 8192
[qwen3-embed]                         ; a section that is not a file: give it a model path
model = /home/jpk/ai/models/llm/Qwen3-Embedding-0.6B-Q8_0.gguf
embedding = true
load-on-startup = true                ; keep it up alongside whatever chat model is active
```

Sections are argument names without dashes; `load-on-startup` and `stop-timeout` are preset-only.
Command-line flags beat the model section, which beats `[*]`.

**Registering the router with LoadCoach** is possible — `[providers.router] kind =
"openai_compatible" base_url = "http://127.0.0.1:8080/v1" remote = false` — but it gives up what
`kind = "llamacpp"` buys: identity by hash (an OpenAI-compatible model is `name_only` confidence
everywhere it surfaces), adapter hot-swap, VRAM-aware admission and residency. Use it for a
model LoadCoach should merely *know about*, not for the ones it schedules.

**llama-swap** ([mostlygeek/llama-swap](https://github.com/mostlygeek/llama-swap)) is the
third-party proxy whose configuration has literal `groups`; use it if you want the router's
behaviour with explicit "these load together / these are exclusive" rules for non-suite clients:

```yaml
models:
  coder:  { cmd: "llama-server --port ${PORT} -m ~/ai/models/llm/qwen2.5-coder-14b-instruct-q4_k_m.gguf -ngl 99 -c 16384" }
  chat:   { cmd: "llama-server --port ${PORT} -m ~/ai/models/llm/gemma-4-12B-it.Q4_K_M.gguf -ngl 99 -c 8192" }
  embed:  { cmd: "llama-server --port ${PORT} -m ~/ai/models/llm/Qwen3-Embedding-0.6B-Q8_0.gguf --embedding" }
groups:
  resident:  { swap: false, exclusive: false, persistent: true, members: [embed] }   # always up
  big:       { swap: true,  exclusive: true,  members: [coder, chat] }               # one at a time
```

Neither router mode nor llama-swap is used by any suite application; they coexist with LoadCoach
as long as they stay off ports 8180–8189 and are not running during a FreeWeight benchmark.

## 7. Model suggestions for a 16 GB card

Licences checked on the model cards on 2026-09-08; **re-check before commercial use**, and treat
"Gemma Terms" and "Llama Community License" as commercial-permitted-with-conditions rather than
open-source. Sizes are the named quantisation's file size, approximately.

| Use | Model (repository) | Licence | Size | Notes |
|---|---|---|---:|---|
| Embedding | `Qwen/Qwen3-Embedding-0.6B-GGUF` (also 4B, 8B) | Apache-2.0 | 0.6 GB | Best small multilingual embedder; instruction-aware queries. Run with `--embedding`. |
| Embedding | `nomic-ai/nomic-embed-text-v1.5-GGUF` | Apache-2.0 | 0.3 GB | 8k context, Matryoshka dims; needs the `search_document:` / `search_query:` prefixes. |
| Embedding | BAAI `bge-m3` (community GGUF, e.g. `gpustack/bge-m3-GGUF`) | MIT | 1.2 GB | Multilingual, 8k, strong on retrieval benchmarks. |
| Embedding | `google/embeddinggemma-300m` (GGUF from `ggml-org`) | Gemma Terms | 0.3 GB | Smallest useful; fine for on-device. |
| Embedding | `Snowflake/snowflake-arctic-embed-l-v2.0` (community GGUF) | Apache-2.0 | 0.6 GB | Retrieval-tuned, English-first. |
| Coding | `Qwen/Qwen2.5-Coder-7B-Instruct-GGUF` `q8_0` | Apache-2.0 | 8.1 GB | The safe default; fits with 32k context. |
| Coding | `Qwen/Qwen2.5-Coder-14B-Instruct-GGUF` `q4_k_m` | Apache-2.0 | 9.0 GB | Noticeably better; 16k context. |
| Coding, agentic | `mistralai/Devstral-Small-2507` (GGUF from `mistralai` / `unsloth`) `Q4_K_M` | Apache-2.0 | 14.3 GB | Tuned for tool-driven coding agents; tight — 4–8k context, or `q8_0` KV. |
| Coding, MoE | `Qwen/Qwen3-Coder-30B-A3B-Instruct-GGUF` `Q4_K_M` | Apache-2.0 | 18 GB | 3 B active: fast even with a third of the layers on CPU. |
| Tools + reasoning | `ggml-org/gpt-oss-20b-GGUF` | Apache-2.0 | 12 GB | Native tool calling and adjustable reasoning effort; the suite's PromptCadence journeys were exercised on it (`gpt-oss:20b`). |
| Tools + general | `Qwen/Qwen3-14B-GGUF` `Q4_K_M` (or Qwen3.5-9B `Q8_0`) | Apache-2.0 | 9 GB | Thinking on/off per request; reliable JSON. IdeaPress's shipped stage defaults name the 9B. |
| General chat, prose | `google/gemma-4-12b-it` (GGUF from `ggml-org`) `Q4_K_M` | Gemma Terms | 7.5 GB | IdeaPress's prose default; add its `mmproj` for vision. |
| General chat | `mistralai/Mistral-Small-3.2-24B-Instruct-2506` `Q4_K_M` | Apache-2.0 | 14 GB | Strongest fully open chat model that fits; short context on 16 GB. |
| General, small | `microsoft/Phi-4-mini-instruct` (GGUF from `unsloth`) `Q8_0` | MIT | 4 GB | Fast fallback tier; good instruction following for its size. |
| General | `meta-llama/Llama-3.1-8B-Instruct` (GGUF from `bartowski`) `Q8_0` | Llama 3.1 Community | 8.5 GB | The quickstarts' `llama3.1:8b`; wide tooling support. |
| Reasoning | `deepseek-ai/DeepSeek-R1-0528-Qwen3-8B` (GGUF from `unsloth`) `Q8_0` | MIT | 8.7 GB | Long chains of thought; cap `max_output_tokens`. |
| Reranking | `BAAI/bge-reranker-v2-m3` (community GGUF) | Apache-2.0 | 0.6 GB | Run with `--reranking`; pairs with bge-m3. |

**Avoid for commercial use:** `jinaai/jina-embeddings-v3` (CC-BY-NC-4.0), any "heretic" /
"uncensored" community merge (licence inherits from the base, provenance does not — and
IdeaPress's `data_classification` join treats an adapter's training data as its own).

**Nothing in the suite calls an embedding endpoint today** (ModelRack declares the
`embedding` capability flag; no application consumes it). Embedding and reranking models are for
your own tooling through §6.

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `loadcoach doctor`: `llama-server not found` | not on `PATH` for the service user | `provider.server_path = "/abs/path/llama-server"` |
| every llama.cpp candidate `insufficient_vram` | another process holds VRAM, or the estimate is right | `nvidia-smi`; kill leaked servers (§5); lower `context_size` or the quant |
| `ModelNotFound … reason = "sharded"` | split GGUF | `llama-gguf-split --merge` |
| first `models refresh` takes a minute | hashing (ADR-0071) | once per file; cached |
| model loads but answers are garbage | broken embedded chat template | no suite key exists yet; rewrite the GGUF's template with `gguf-py/gguf/scripts/gguf_new_metadata.py --chat-template-config` |
| adapter shows `adapter_incompatible` | manifest `base` digest ≠ served base | rescan; the base file changed |
| adapter never routed, pins work | no FreeWeight evidence yet | `freeweight run start` on the adapter subject, export, then `loadcoach evidence import` |
| `PROFILE_MISMATCH` | a running server was launched with other flags | LoadCoach restarts it at next idle; or `loadcoach models residency` and unload |
| CUDA build error on `rsqrt` | CUDA 13.1 + glibc 2.43 | the include overlay in §1 |
