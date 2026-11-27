# Engines, checkpoints and GPU lifecycle

An **engine** is a plugin directory under `engines/<engine_id>/` containing an
`engine.yaml` manifest and a `worker.py` that speaks IPC protocol v1. The daemon
never imports model code; it spawns a worker lazily, routes work to it, and reaps
it when idle. Adding an engine never requires touching the daemon.

Six engines ship, in **two kinds**:

| Engine id | Kind | Scheduling | Backend | Venv | Streaming | When to use |
|---|---|---|---|---|---|---|
| `qwen3_tts_fast` | `tts` | `gpu` | `faster-qwen3-tts` (CUDA graphs) | own `uv` venv | native PCM | default GPU; fastest, real streaming. **CUDA only** |
| `qwen3_tts` | `tts` | `gpu` | `qwen-tts` (official) | own `uv` venv | emulated | GPU fallback when graph capture misbehaves; also the CPU/MPS/ROCm engine |
| `kokoro` | `tts` | `cpu` | `kokoro==0.9.4` (CPU PyTorch) | own `uv` venv | native PCM | **no GPU** on its shipped `cpu` variant: 82M, 24 kHz, 3.4–5.2× real time |
| `mock` | `tts` | `cpu` | built-in tone generator | core interpreter | native PCM | CPU-only testing of the full routing surface |
| `phonon` | `asr` | `cpu` | `fermion-research==0.2.15` | own `uv` venv | — | **no GPU**: real transcription, 25 languages, ≈42–48× real time |
| `mock_asr` | `asr` | `cpu` | built-in synthetic transcript | core interpreter | — | testing transcription with no deps |

`kokoro` and `mock` declare `device: cpu` in their manifests, so neither takes the
single-GPU-owner slot and neither is killed by the VRAM-pressure reaper. Before v0.6.0
they inherited `gpu` and took that slot while holding 0 MB — a scheduling bug, now fixed.

The two Qwen backends need **mutually exclusive** dependency universes
(`transformers==4.57.3` for `qwen-tts`, `transformers==5.15.1` for
`faster-qwen3-tts`), which is why they are separate plugins with separate venvs.
They cannot share an environment. `kokoro` is separate for a different reason: its
shipped venv pins CPU-only torch, so it cannot see a CUDA device even on a GPU box.
`phonon` is separate for that same reason plus the opposite job.

## Two manifest fields: `kind` and `device`

They decide different things and both default to the v0.4 behaviour, so every older
manifest keeps loading unchanged:

| Field | Values | What it decides |
|---|---|---|
| `kind` | `tts` (default) · `asr` | Which IPC method the daemon calls — `synthesize`/`stream_pcm` versus `transcribe` — and which conformance table `engines test` walks. Routing refuses a wrong-kind request with `BAD_REQUEST` before a worker spawns |
| `device` | `gpu` (default) · `cpu` | **Scheduling**, not routing. A `cpu` engine is exempt from the single-GPU-owner slot: `_begin_job` never acquires it, so a transcription runs while a TTS model is resident instead of evicting it. The VRAM-pressure reaper also leaves `cpu` workers alone |

v0.6.0 adds two **runtime-device** fields alongside them, and they are deliberately not
the same field:

| Field | Values | What it decides |
|---|---|---|
| `supported_devices` | `[cpu]` by default; any of `cpu`, `cuda`, `mps`, `rocm` | Which runtime devices the engine's backend can serve. Used to validate `AGENT_VOICE_DEVICE`/`AGENT_VOICE_DEVICES` and `engines install --device`; a request for a device outside this list is a `BAD_REQUEST` naming the supported list |
| `default_device` | `cpu`, `cuda`, `mps`, `rocm`, or `null` | What `auto` resolves to for this engine before the host's accelerator is considered |

Declare `device: cpu` honestly. A `cpu` engine that grabs a CUDA context breaks the
"0 MB VRAM" promise an operator planned around; a `gpu` engine marked `cpu` would
hold no slot while competing for the card anyway. Declare `supported_devices` honestly
too — it is what stops `AGENT_VOICE_DEVICE=cuda` from silently installing a torch build
the engine's package cannot use.

## Device variants

A device is **not** a `uv sync` flag. Each committed `uv.lock` bakes the torch wheel
index into every torch entry, so the shipped locks already disagree about which torch
they hold — and the variants are different dependency sets by construction:

| Engine / variant | torch | torchaudio | index |
|---|---|---|---|
| `qwen3_tts` canonical (`cuda`) | `2.11.0+cu128` | `2.11.0+cu128` | `https://download.pytorch.org/whl/cu128` |
| `qwen3_tts` `variants/cpu` | `2.15.0+cpu` | `2.11.1+cpu` | `https://download.pytorch.org/whl/cpu` |
| `kokoro` canonical (`cpu`) | `2.15.0+cpu` | — | `https://download.pytorch.org/whl/cpu` |
| `kokoro` `variants/cuda` | `2.11.0+cu128` | — | `https://download.pytorch.org/whl/cu128` |
| `kokoro` `variants/mps` | `2.15.0` (PyPI, `macosx_14_0_arm64`) | — | PyPI |
| `phonon` canonical (`cpu`) | `2.15.0+cpu` | — | `https://download.pytorch.org/whl/cpu` |
| `phonon` `variants/mps` | `2.15.0` (PyPI, `macosx_14_0_arm64`) | — | PyPI + `fermion-research[mlx]` |
| `qwen3_tts` `variants/rocm` (measured) | `2.15.0+rocm7.14` | `2.11.1+rocm7.14` | `https://download.pytorch.org/whl/rocm7.14` |
| `qwen3_tts` `variants/mps` (measured) | `2.15.0` (PyPI, `macosx_14_0_arm64`) | `2.11.1` | PyPI |

Do **not** expect the variants to hold the same torch: the CPU index lags on
torchaudio, so the CPU pair is `torch 2.15.0+cpu` / `torchaudio 2.11.1+cpu` — a
declared, visible difference, not a bug. The exact measurements are in
[phase0-devices-report.md](phase0-devices-report.md).

Rows marked *(measured)* come from the Phase 0 throwaway copy. What this checkout can
actually install is what lives under `engines/<id>/variants/`. Run
`agent-voice engines install <id> --device <d> --dry-run` to see the resolved project
root, and note that a pair with no shipped variant project or lock **refuses to
install** — `--experimental` cannot conjure a lock.

### The proven index URLs

| Variant | Index | Notes |
|---|---|---|
| `cpu` | `https://download.pytorch.org/whl/cpu` | `explicit = true` is fine — the index serves only torch/torchaudio |
| `cuda` | `https://download.pytorch.org/whl/cu128` | the canonical project for the Qwen engines |
| `rocm` | `https://download.pytorch.org/whl/rocm7.14` | must be **non-`explicit`** (torch's `rocm[device-all]` and `triton-rocm` live on the same index) **and** narrowed with `[tool.uv] environments = ["sys_platform == 'linux' and platform_machine == 'x86_64'"]`. `rocm10.0` also carries torch 2.15.0 but is undocumented, so it is not used |
| `mps` | none — PyPI's macOS arm64 wheel *is* the MPS build | narrowed with `[tool.uv] environments = ["sys_platform == 'darwin' and platform_machine == 'arm64'"]` |

Two rules that come straight out of the measurements, and that a new variant must obey:

1. **A non-PyPI index must declare `[tool.uv] environments`** for the platforms it
   actually serves. Without it uv's universal resolution backtracks to an ancient torch
   that has broad wheel coverage — measured: `rocm7.14` → `torch 2.0.1`, a lock with
   zero wheels.
2. **Only an index that serves nothing but torch/torchaudio may be `explicit = true`.**
   ROCm cannot be, because torch's own runtime dependencies are published on that same
   index. A non-explicit extra index must be checked per package — an extra index can
   remap a transitive dependency such as `networkx`.

**Intel macOS is excluded.** `fermion-research` pins `torch>=2.2,<2.3` on Intel macOS,
which cannot coexist with Phonon's `torch==2.15.0` CPU pin, so
`engines/phonon/pyproject.toml` limits uv's resolution environments to Linux, Windows
and Apple silicon. Intel Macs are not a supported Phonon host, and no variant changes
that.

### Layout: one venv, several lock roots

```text
engines/<id>/
├── pyproject.toml              # canonical variant; [tool.agent-voice] device = "<default>"
├── uv.lock                     # the canonical lock — never rewritten by an install
├── .venv                       # THE one venv, created and populated by the sync below
├── .variant                    # stamp: device + torch version, written by install
└── variants/<device>/
    ├── pyproject.toml          # same deps, different torch index, [tool.agent-voice] device = "<device>"
    └── uv.lock
```

`engines install <id> --device <d>` resolves the project directory (the canonical one
when its `[tool.agent-voice] device` already matches, otherwise `variants/<device>/`)
and runs exactly:

```python
subprocess.run(
    [uv, "sync", "--project", str(project_dir), "--locked"],
    env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(engine_dir / ".venv")},
    cwd=str(project_dir),
    shell=False,
    check=True,
)
```

Consequences worth knowing:

- The venv stays at the **canonical** `engines/<id>/.venv` path that `engine.yaml`
  already points at, so nothing else in the daemon changes. No `.venv` is created under
  `variants/<device>/`.
- `--locked` is mandatory, not an optimisation: without it a `uv sync` against a drifted
  variant **silently rewrites the committed lock** (measured: the sha changed and a
  package was added). With it, drift is refused with exit code 1 and the lock is
  untouched.
- **Switching a device replaces that engine's venv contents in place** — `uv sync`
  installs and prunes to exactly the variant's lock, so a cuda→cpu switch leaves no
  `nvidia-*` or `triton` directories behind. Only **one variant per engine** is resident
  at a time. On a cold uv cache the same switch is a full download: the CPU venv is
  1.6 GB and the CUDA venv is 7.3 GB.
- An `--index`, `--default-index`, `--index-url`, `UV_INDEX` or `UV_DEFAULT_INDEX` can
  **never** select a device: `[tool.uv.sources]` wins over all of them (seven measured
  variants of the flag, seven identical results), and on the real CUDA project `--index`
  silently downgraded torch while desynchronising torchaudio. `engines install` never
  passes them and strips them from the environment.
- The manager runs a torch probe on the installed engine: if the venv holds `torch
  2.15.0+cpu` while the resolved device is `cuda`, the engine answers
  `409 ENGINE_NOT_INSTALLED` naming `agent-voice engines install <id> --device cuda` —
  a clear refusal, not a worker crash. That is the "stale variant" case.

### Flags

```bash
agent-voice engines install qwen3_tts --device cpu                 # verified pair
agent-voice engines install kokoro --device cuda --experimental    # experimental pair
agent-voice engines install qwen3_tts --device cuda                # the canonical project
```

| Flag | Effect |
|---|---|
| `--device auto\|cpu\|cuda\|mps\|rocm` | Which variant to sync. `auto` (the default) keeps the canonical project, or the variant the resolved device selects |
| `--experimental` | Allow an `experimental` pair. Never unlocks an `unsupported` one |
| `--dry-run` | Print the resolved project root and the exact `uv sync` argv, then exit — writes nothing |

A pair is installable only if a variant **project and lock exist** for it
(`engines/<id>/variants/<device>/` or a canonical project whose
`[tool.agent-voice] device` matches). `--experimental` grants permission; it cannot
create a lock. So:

```text
$ agent-voice engines install qwen3_tts_fast --device rocm --experimental
BAD_REQUEST: engine 'qwen3_tts_fast' does not support device 'rocm'
Supported devices: cuda.
```

That is the honest gate: `qwen3_tts_fast` is CUDA-only, and a supported pair whose
variant project or lock is missing is refused with `Available variants: …` rather than
silently resolving a different dependency set. Shipped roots today:
`qwen3_tts` cpu/cuda/mps/rocm, `kokoro` cpu/cuda/mps, `phonon` cpu/mps.
`--dry-run` is the cheapest way to see which projects this checkout has. `engines install`
never touches another engine's venv.

### The support matrix, per pair

`DEVICE_SUPPORT` in [`../src/agent_voice/hardware.py`](../src/agent_voice/hardware.py) is
the single source of truth; this table is a copy of it. **verified here** = installed and
run end to end on the reference host (Linux, RTX 4060 Ti 16 GB) or in a named prior
report; **experimental** = wired, lock producible, never run on that hardware;
**unsupported** = the package does not claim it.

| Engine | `cpu` | `cuda` | `mps` | `rocm` |
|---|---|---|---|---|
| `kokoro` | **verified here** | experimental | experimental | unsupported |
| `phonon` | **verified here** | unsupported — container only | experimental | unsupported |
| `qwen3_tts` | **verified here** | **verified here** | experimental | experimental |
| `qwen3_tts_fast` | unsupported — CUDA-only package | **verified here** | unsupported | unsupported |
| `mock` | **verified here** | unsupported | unsupported | unsupported |
| `mock_asr` | **verified here** | unsupported | unsupported | unsupported |

Why the refusals are refusals:

- **`qwen3_tts_fast` is CUDA-only.** Its METADATA says *"The Torch backend requires an
  NVIDIA GPU with CUDA"*, and its graph code calls `torch.cuda.CUDAGraph()`
  unconditionally. There is no ROCm or HIP claim anywhere in the distribution — the
  "ROCm notes" comment this repo used to carry was unsourced and is gone. Its only
  non-NVIDIA path is the GGML/Metal backend, which is not shipped here.
- **`phonon` has no CUDA wheel.** NVIDIA support is the
  `ghcr.io/fermionresearch/phonon-cuda` **container**, not a package extra, and
  `fermion.load_speech` takes no device kwarg to pass. Its Apple path is the `mlx` extra,
  exposed here as `mps` (`FERMION_DEVICE=mlx`) and experimental.
- **`mock`/`mock_asr` are CPU by construction** — no dependencies, no torch, always
  `gpu_vram_mb: 0`.

### What the 0 MB claim covers

Only `device=cpu`. The shipped `kokoro` and `phonon` venvs hold CPU torch, set
`CUDA_VISIBLE_DEVICES=""` before the torch import and report `gpu_vram_mb: 0`; that is a
verified property of the `cpu` variant. Their `cuda`/`mps` paths are **experimental** and
report real memory (`torch.cuda.memory_allocated()` / `torch.mps.current_allocated_memory()`),
so the existing 0 MB assertions apply to the CPU path only.

## Speech in: the transcribe path

A `kind: asr` engine answers `transcribe` instead of `synthesize`:

```text
transcribe  req  {"model_alias", "audio_path", "language", "word_timestamps", "hotwords"}
            resp {"model_alias", "backend_name", "backend_version", "language",
                  "duration_sec", "text", "truncated",
                  "segments": [{"index", "start_sec", "end_sec", "text", "words"?}]}
```

The daemon extracts a **16 kHz mono WAV** once with ffmpeg and hands the worker that
path, so no worker ever decodes a container. Segments group on sentence punctuation,
a pause `>= 0.8 s`, and a 7 s cap — the shape subtitles and dubbing both rely on.
`words[]` is populated only when `word_timestamps` was asked for, and `hotwords` is a
decoder bias list capped at 25 terms.

Two doors reach it: `POST /v1/agent/transcribe` (a confined local path) and
`POST /v1/audio/transcriptions` (OpenAI multipart). The CLI is
`agent-voice transcribe <file>`, and MCP is `transcribe()`.

Dubbing builds on both: `dub_prepare` transcribes and extracts a bed plus a speaker
reference; the caller translates; `dub_render` speaks the translated lines into their
original slots. See [api.md](api.md#dubbing).

## Five checkpoints, three modes

Every alias is a separate Qwen3-TTS 12Hz checkpoint, and its `mode` decides what
it can do:

| Alias | Params | VRAM | Mode | Reference clip? | Instruct? | Use it for |
|---|---|---|---|---|---|---|
| `1.7b-base` | 1.7B | ~4.4 GB | `clone` | required | no | default; best cloning quality |
| `0.6b-base` | 0.6B | ~2.2 GB | `clone` | required | no | `quality="fast"` cloning |
| `1.7b-custom` | 1.7B | ~4.4 GB | `custom_voice` | not needed | yes | nine preset speakers, plus `instruct` |
| `0.6b-custom` | 0.6B | ~2.2 GB | `custom_voice` | not needed | no | nine preset speakers, no `instruct` |
| `1.7b-design` | 1.7B | ~4.4 GB | `voice_design` | not needed | yes | design a voice from a description |

Kokoro adds a fourth engine with a single checkpoint: `kokoro-82m` in
`custom_voice` mode, `est_vram_gb: 0.0`, eight preset speakers and no clone or
design mode — see [Kokoro-82M](#kokoro-82m-the-cpu-engine).

The `mode` field is the routing contract:

| `mode` | Speaks | Reached by | Needs a reference clip |
|---|---|---|---|
| `clone` | a reference clip | `voice=<id or name>`, or nothing when a voice is registered | yes |
| `custom_voice` | fixed named timbres | `speaker="Ryan"` | no |
| `voice_design` | a timbre invented from words | `design_voice(instruct="…")` or `speak(instruct="…")` with no voice/speaker | no |

Routing happens before a worker is spawned, so a `speaker=` request never lands
on a checkpoint that can only clone. `speak` refuses `voice` and `speaker` passed
together — two timbres in one request is an error, not a guess.

```bash
uv run agent-voice engines install qwen3_tts_fast
uv run agent-voice models download qwen3_tts_fast 1.7b-base
uv run agent-voice models download qwen3_tts_fast 0.6b-base
uv run agent-voice models download qwen3_tts_fast 1.7b-custom
uv run agent-voice models download qwen3_tts_fast 1.7b-design
uv run agent-voice models list
```

Weights land in the standard Hugging Face cache and are reused if already
present. `models list` reports `downloaded_locally` only when real weight files
exist — a bare `refs/` pointer is not a usable model. It also reports each alias's
`mode`, `supports_instruct` and `preset_speakers`, so an agent can tell which
model answers a request without reading the manifest.

### Nine preset speakers

The CustomVoice checkpoints ship nine fixed timbres. Pass one as `speaker=` and
no reference clip is needed at all:

`Vivian` · `Serena` · `Uncle_Fu` · `Dylan` · `Eric` · `Ryan` · `Aiden` · `Ono_Anna` · `Sohee`

```python
speak("Good evening.", speaker="Serena")                                  # 0.6b-custom is enough
speak("Good evening.", speaker="Uncle_Fu", instruct="slower, wearier")    # needs 1.7b-custom
```

`instruct` is honoured only by checkpoints that declare `supports_instruct` —
`1.7b-custom` and `1.7b-design`. `0.6b-custom` speaks the same nine timbres with
no instruct support. Names are matched case-insensitively and canonicalised to
the manifest's spelling (the speaker is part of the cache key); an unknown name
is `BAD_REQUEST` and never reaches the backend.

`server_info().preset_speakers` reports what the **default engine** can voice
right now. Speaker lists for another engine come from `list_models(engine_id)`.

### Design a voice from a description

`design_voice` turns words into a speaker with no reference audio at all:

```python
design_voice(
    name="narrator",
    instruct="warm baritone, measured pace, a little gravel, late-night radio",
)
```

The result carries `preview_file_path` and, with the default `save_as_clone=true`,
a `voice_id` — the render is registered as an ordinary reference voice. From then
on it is a normal clone voice on **any** cloning checkpoint, which is the
design→clone workflow:

```python
speak("Chapter one.", voice="narrator")     # 0.6b-base or 1.7b-base, your pick
```

`list_voices()` reports each voice's `source` — `cloned` from a clip, or
`designed` — and, for a designed one, the `design_instruct` it was built from, so
provenance survives.

Pass `save_as_clone=false` to audition a design without registering it: you get
the preview and `voice_id: null`. `take=2`, `take=3`, … re-rolls the same
description without touching take 1, and `seed=` makes a design reproducible.

Designing runs on the `1.7b-design` checkpoint, so download it first;
`server_info().voice_design_available` tells you whether it is already there.

#### Audition several takes at once

`candidates=2..5` renders that many takes in a single call and registers none of
them — it forces `save_as_clone=false`:

```python
design_voice(
    name="narrator",
    instruct="warm baritone, measured pace, a little gravel, late-night radio",
    candidates=3,
)
```

The result has `voice_id: null` and a `candidates` list with `take`, the `seed`
behind that take, `file_path`, `download_url` and `duration_sec` for each render.
Take 1 is also `preview_file_path`, so a caller that ignores the list still has
audio to play. Each take uses a take-derived seed, so every candidate is a
genuinely different delivery that stays reproducible.

Pick one and register exactly that render:

```python
design_voice(name="narrator", instruct="…", take=2, save_as_clone=True)
```

Omit `seed`: the take-derived seed is recomputed, the request matches the winning
candidate's cache key, and the voice is registered from the cached audio instead
of being synthesised again.

From a shell, no daemon required — the command starts the worker manager
in-process:

```bash
uv run agent-voice voices design narrator \
  --instruct "warm baritone, measured pace, a little gravel, late-night radio"
```

`--candidates 2..5` renders takes to audition and saves none; re-run with
`--take <n> --save-as-clone` to keep the winner.

### Measured on an RTX 4060 Ti 16 GB

| Metric | Value |
|---|---|
| Daemon boot with a GPU engine configured | **0 workers, 631 MiB VRAM** (= desktop idle) |
| Cold start (spawn + load 0.6B + 9.5 s speech) | 21.8 s |
| Warm request (model resident, 9.0 s speech) | 10.5 s |
| Warm realtime multiple (9.0 s audio / 10.5 s wall) | **≈ 0.86** — slightly slower than real time |
| Cache hit (identical request) | 0.01 s |
| VRAM with 0.6B loaded | ~3.5 GB |
| VRAM after `unload(force=true)` | **639 MiB** — back to baseline |

Measured with the official `qwen3_tts` backend, not `qwen3_tts_fast`. Full
numbers and dependency findings: [phase0-report.md](phase0-report.md).

## Kokoro-82M: the CPU engine

`kokoro` is the zero-GPU engine: a real neural TTS model (82M parameters,
Apache-2.0) that runs on the CPU at 3.4–5.2× real time, uses **0 MB of VRAM** and
emits native 24 kHz mono audio. On its shipped `cpu` variant nothing about it touches
CUDA: the manifest declares `device: cpu` and `supported_devices: [cpu, cuda, mps]`, the
engine venv installs torch from the `pytorch-cpu` index, and the worker sets
`CUDA_VISIBLE_DEVICES=""` before importing torch and passes `device="cpu"` to
`KPipeline` (whose default would auto-select CUDA on a GPU box). The `cuda` and `mps`
variants are **experimental**: they swap the venv for a GPU build, make
`CUDA_VISIBLE_DEVICES=""` conditional, pass the resolved device through, and report real
memory instead of 0 MB.

| | |
|---|---|
| Model | `kokoro-82m` — `hexgrad/Kokoro-82M` (Apache-2.0), 82M parameters |
| Backend | `kokoro==0.9.4` (Apache-2.0), CPU-only PyTorch on the `cpu` variant, `engines/kokoro/.venv` |
| Weights | 363 MB repo, 327 MB `kokoro-v1_0.pth`, fetched through the HF cache |
| Native rate | 24,000 Hz mono int16 — no resampling |
| Devices | `cpu` **verified here** · `cuda` experimental · `mps` experimental · `rocm` unsupported |
| VRAM | **0 MB on `device=cpu`**: `gpu_vram_mb: 0`, `torch.version.cuda is None`, no `nvidia-*` wheel |
| Modes | `custom_voice` only — no clone, no design, no `instruct` |
| Languages | `Auto`, `English` |
| Streaming | native: one chunk per pipeline result, cancelled between chunks |
| Measured | 3.41× / 5.23× / 4.71× real time, 1.37 GB peak RSS, 2.05 s warm load |

### Install and verify

```bash
uv run agent-voice engines install kokoro
uv run agent-voice models download kokoro kokoro-82m   # 363 MB, once
uv run agent-voice doctor                               # espeak-ng row + weights present
uv run agent-voice engines test kokoro                  # 7/7 IPC v1 conformance
```

`engines test` is **mode-aware**: an engine with no clone checkpoint is exercised
through its first `preset_speakers` entry, with `mode` and `speaker` sent on both
the synthesize and the stream step — exactly what `speak(speaker=...)` does. A
clone-only or clone-first engine keeps the original v1 path unchanged.

### The eight voices

Kokoro ships 54 voices across 8 languages upstream; the manifest advertises the
eight English voices that were verified end to end:

`af_heart` · `af_bella` · `af_sarah` · `am_adam` · `am_michael` · `bf_emma` · `bm_george` · `bm_lewis`

`a` = American English, `b` = British English; the first letter is also Kokoro's
`lang_code`. Pass one as `speaker=` — there is no reference clip to supply or
reuse:

```python
speak("Zero GPU, twenty-four kilohertz.", speaker="af_heart")
```

An OpenAI-shaped client keeps working without changes: `alloy` → `af_heart`,
`echo` → `am_adam`, `nova` → `af_bella`, `onyx` → `am_michael`, `fable` →
`bm_george`, `shimmer` → `bf_emma`.

### espeak-ng and the spaCy G2P model

Kokoro's English G2P is spaCy-based (`en_core_web_sm`); `espeak-ng` is the
out-of-dictionary fallback and the backend for every non-English voice. Common
English works without it, so `doctor` reports a missing binary as a **WARN with the
platform install command** — never a FAIL:

```bash
sudo apt install espeak-ng     # Debian/Ubuntu
brew install espeak-ng         # macOS
```

Both prerequisites are pinned in `engines/kokoro/pyproject.toml`. `en-core-web-sm`
is a **direct URL dependency** because misaki's own installer picks the *active*
interpreter rather than the engine venv, and `torch` is a **direct dependency with
an explicit CPU index** because a transitive torch resolves the CUDA wheel and
drags in every `nvidia-*` package. The exact lock is in
[phase0-kokoro-report.md](phase0-kokoro-report.md).

### What Kokoro cannot do

No `voice_clone`, no `voice_design`, no `instruct_control`. A cloned voice or a
designed timbre is refused before a worker spawns — use a Qwen checkpoint for
those. It is English-only for now: other Kokoro languages need the extra `misaki`
extras and an espeak-ng verification pass. The lighter `kokoro-onnx` backend is
documented as the alternative but is not shipped: its weights are GitHub release
assets, not Hugging Face ones, so `models download` and `hf_model_downloaded()`
would need a custom downloader.

## Phonon: the CPU ASR engine

`phonon` is the listening half of the daemon and the first shipped `kind: asr`
engine. It is a real speech recogniser — a 0.6B FastConformer/TDT model compressed
to int6 — that runs on the **CPU** at ≈42–48× real time, uses **0 MB of VRAM**
and comes from a **156 MB** download. It declares `device: cpu` and
`default_device: cpu`, so it is exempt from the GPU owner slot: transcribing while a
Qwen model is resident does not evict it.

| | |
|---|---|
| Models | `phonon-2.5` (default, 25 languages, 156 MB), `phonon-2.5-en` (English, 158 MB), `phonon-2` (164 MB, `quality_tier: fast`) |
| Backend | `fermion-research==0.2.15`, CPU-only PyTorch, `engines/phonon/.venv` |
| Licence | **CC-BY-4.0 weights** (derived from NVIDIA Parakeet TDT 0.6B v3), **Apache-2.0 code**. Attribution is required on redistribution; the model card and `NOTICE` carry it |
| Devices | `cpu` **verified here** · `mps` experimental (the `mlx` extra, `FERMION_DEVICE=mlx`) · `cuda` **unsupported** — the NVIDIA path is a container · `rocm` unsupported |
| VRAM | **0 MB on `device=cpu`**: `CUDA_VISIBLE_DEVICES=""` before the torch import, `gpu_vram_mb: 0`, no `nvidia-*` wheel |
| Modes | `asr`. No speech output at all — `synthesize` on it is `BAD_REQUEST` |
| Languages | 25 with `phonon-2.5`: en, es, fr, de, it, pt, nl, pl, ru, uk, ro, cs, sk, bg, hr, sv, da, fi, et, lv, lt, hu, sl, mt, el |
| Capabilities | `word_timestamps: true`, `hotwords: true`, `multilingual: true` |
| Measured | 39.4–47.9× real time across nine runs on a Ryzen 7 5700X, ≈1.00 GB peak RSS, 1.05 s warm load |

Phonon's `pyproject.toml` limits uv's resolution environments to Linux, Windows and
Apple silicon. Fermion pins `torch>=2.2,<2.3` on Intel macOS, which cannot coexist with
this engine's `torch==2.15.0` CPU pin; **Intel Macs are not a supported Phonon host**, and
no device variant changes that. The only other variant is `mps` — the `mlx` extra,
mapped to `FERMION_DEVICE=mlx`, experimental and never executed on Apple silicon. There
is no `cuda` variant to install: `fermion`'s NVIDIA path is the
`ghcr.io/fermionresearch/phonon-cuda` container, not a wheel, and `load_speech` takes no
device kwarg.

Phonon has **no per-request language control** on 2.5: it auto-detects, and the
worker reports `"auto"` when the backend says nothing. It also has **no confidence
field**, so neither is exposed. `phonon-2.5-en` is a fixed-English checkpoint
instead.

### Install and verify

```bash
uv run agent-voice engines install phonon
uv run agent-voice models download phonon phonon-2.5     # 156 MB, once
uv run agent-voice doctor                                 # `asr` row + weights present
uv run agent-voice engines test phonon                    # 7/7 IPC v1, 0 MB VRAM
```

`engines test` walks the **ASR table**: `spawn+connect → hello → load_model →
transcribe → cancel → health → shutdown`. The `transcribe` row sends a 1 s 16 kHz
tone and passes on a well-formed reply (a `segments` list and a string `text`),
never on words. The `cancel` row starts a 20 s transcribe, waits for the worker to
report a request in flight, then cancels and passes on a well-formed
`{cancelled: bool}` — `cancelled: false` included, because a CPU decode can finish
before the round trip. The detail string says which happened.

### The first call is slow

`load_speech` unpacks `~/.cache/fermion/speech/FermionResearch__Phonon-2.5/` on the
first use: **50.1 s** measured, including a 0.16 GB download and a per-member
SHA-256 verification. Every later load is **1.05 s**. `load_model` has a 600 s
budget for exactly this reason — do not lower it.

Measured numbers and the exact dependency recipe:
[phase0-asr-report.md](phase0-asr-report.md).

## Adding an engine

The full contract is in
[`engines/ADDING_AN_ENGINE.md`](../engines/ADDING_AN_ENGINE.md). In short:

```text
engines/<engine_id>/
├── engine.yaml      # required — declarative manifest
├── worker.py        # required — speaks IPC protocol v1
└── pyproject.toml   # optional — only if the engine needs its own dependencies
```

```yaml
engine_id: example_tts
display_name: Example engine
backend_name: example-tts     # PyPI distribution name — part of the cache key
backend_version: "1.2.3"      # must match what is installed — part of the cache key
protocol_version: 1
venv_python: .venv/bin/python # null => run in the core interpreter (zero-dep engines)
kind: tts                     # tts (default) | asr
device: gpu                   # gpu (default) | cpu — cpu never takes the GPU slot

capabilities:
  streaming: true             # true only if PCM arrives during decode
  pcm_native: true            # true only if it emits 24 kHz mono int16 directly
  voice_clone: false
  custom_voice: true
  voice_design: false
  instruct_control: false
  languages: [English]

models:
  - alias: example-1
    hf_repo: local/example-1  # a local/ repo counts as present without a download
    quality_tier: fast        # high | fast
    est_vram_gb: 0.0
    default: true
    mode: custom_voice        # clone | custom_voice | voice_design
    supports_instruct: false
    preset_speakers: [Ash, Blair]

supported_devices: [cpu]      # runtime devices the backend can serve
default_device: cpu           # what `auto` resolves to before the host is considered
```

An ASR engine swaps `kind: asr` / `device: cpu` in, drops `mode`/`preset_speakers`,
and adds `word_timestamps`/`hotwords`/`multilingual` to `capabilities`. The full
walkthrough, with a complete `faster_whisper` sketch, is in
[`engines/ADDING_AN_ENGINE.md#6-adding-an-asr-engine`](../engines/ADDING_AN_ENGINE.md#6-adding-an-asr-engine).

`supported_devices` and `default_device` are new in v0.6.0 and are **required keys in
practice**: `EngineManifest` forbids unknown keys, so a manifest carrying a typo silently
drops the engine instead of failing loudly. Defaults are `["cpu"]` and `null`, which is
why every older manifest keeps loading; a GPU engine should declare them explicitly.
Adding a non-default variant also means adding `variants/<device>/{pyproject.toml,uv.lock}`
with `[tool.agent-voice] device = "<device>"`, and following the two index rules in
[Device variants](#device-variants) — a variant whose lock cannot be produced is not
installable and the pair stays `unsupported`.

The shipped `engines/kokoro/engine.yaml` is the worked example of a CPU **TTS**
engine: `device: cpu`, `est_vram_gb: 0.0`, `voice_clone: false`, one
`custom_voice` model, eight preset speakers. `engines/phonon/engine.yaml` is the
ASR one. The shipped Qwen manifests are the worked example of the three-mode
routing surface.

Rules that keep the system honest:

- `backend_name` / `backend_version` must be accurate and knowable **before** the
  worker starts — they are in the cache key. If they lie, stale audio gets reused.
- `capabilities` describes the engine; each model's `mode`,
  `supports_instruct` and `preset_speakers` refine it. An engine that declares
  `voice_design: true` but ships no `voice_design` alias fails callers with
  `MODEL_NOT_FOUND` instead of a clear answer.
- `preset_speakers: []` on a `custom_voice` model means "accept any name", which
  is only right if the backend validates the name itself.
- `kind` and `device` are declarations, not hints. `kind: asr` puts the engine in
  the transcription family; `device: cpu` exempts it from the GPU owner slot. Say
  `device: cpu` only if the worker really allocates no VRAM.
- `supported_devices` is a claim about the **backend**, `device` is about
  **scheduling**, and they are different fields on purpose. Declaring `cuda` in
  `supported_devices` promises a variant that installs and runs; if you have not run it
  on that hardware, the pair belongs in `DEVICE_SUPPORT` as `experimental`, not
  `verified`, and `--experimental` is required to install it.
- `worker.py` must keep reading the socket while inference runs, so `cancel` and
  `health` land mid-decode. Copy the parent-death guards
  (`PR_SET_PDEATHSIG` + a 2 s `os.getppid()` poll) or an orphan keeps VRAM.
- Audio is always 24 kHz mono int16 LE. Never build a shell string; argv lists
  with `shell=False` only.
- An ASR worker returns segments grouped on **sentence punctuation**, a pause
  **≥ 0.8 s** and a **7 s** cap, and `words[]` only when `word_timestamps` asked
  for it. `engines/phonon/worker.py::group_words_into_segments` is the reference.

Then install and verify:

```bash
uv run agent-voice engines install <engine_id>
uv run agent-voice engines test <engine_id>   # full IPC v1 conformance, exits non-zero on failure
```

`engines test` walks `spawn+connect → hello → load_model → synthesize → cancel →
health → shutdown` for a TTS engine, and `… → transcribe → cancel → …` for an ASR
one, printing a pass/fail row per step. Skipped steps are `SKIP` with a reason,
never a pass. It picks a clone model first; when an engine has none (Kokoro), it
uses the first `custom_voice` checkpoint's first preset speaker and sends `mode` +
`speaker`, so a preset-only engine reaches 7/7 as well. An ASR engine is exercised
with a 1 s tone and passes on a well-formed reply, not on words. `mock` and
`mock_asr` need no GPU, so they are the reference runners:

```bash
uv run agent-voice engines test mock
uv run agent-voice engines test mock_asr
```

Agent-authored engines go through the same path but are gated: with
`ALLOW_AGENT_ENGINE_MUTATION=false` (default) `register_engine` is refused; with
it enabled, `REQUIRE_ENGINE_APPROVAL=true` stages the plugin in
`engines/.pending/<id>/` until an operator runs `agent-voice engines approve <id>`
(model list and `worker.py` SHA-256 are printed before confirmation; `--yes`
skips the prompt).

## GPU lifecycle

- **0 workers at boot, 0 MB VRAM.** Only the core daemon runs; it holds no model
  code. When a `kokoro` or `phonon` worker *is* resident on `device=cpu` it still
  contributes 0 MB of VRAM — the model lives in system RAM. Their experimental
  cuda/mps variants do allocate, and report what they allocate.
- **`device: cpu` engines never take the GPU slot.** `_begin_job` skips
  `_acquire_gpu` for them, so a transcription runs next to a resident Qwen model
  instead of evicting it, and `gpu_status().active_engine` keeps meaning "the GPU
  owner" rather than "whatever ran last". The VRAM-pressure reaper skips them too.
  Scheduling uses the **resolved** runtime device, not the static `device` field, so
  a `kokoro` switched to `cuda` takes an accelerator slot and a `qwen3_tts` switched
  to `cpu` does not.
- **The cache is consulted before any worker is spawned.** A hit costs a disk
  read and never wakes the GPU. The resolved **device** is part of the cache key:
  a CPU render and a CUDA render of the same text are not bit-identical, so they must
  not share an entry.
- **Workers are lazy and reapable.** A worker spawns on the first uncached request
  for its engine and is reaped after `MODEL_IDLE_TTL_SECONDS` (default 900) idle
  seconds.
- **Engine switching is hysteretic.** With `MAX_ACTIVE_ENGINES=1`, a request for
  a different engine is refused with `409 WORKER_BUSY` while the active engine
  still has queued work ("still draining its queue"), and
  `MIN_ENGINE_RESIDENCY_SECONDS` stops two engines from ping-ponging.
- **`unload_models(force=false)`** returns `409 WORKER_BUSY` while jobs are
  running; `force=true` cancels them and frees VRAM immediately.
- **VRAM pressure yield.** With `ENABLE_VRAM_PRESSURE_YIELD=true` (default), an
  **idle** worker is killed when another application needs the GPU.
- **Admission is bounded.** Uncached jobs queue FIFO per engine; a full wait queue
  (`MAX_QUEUE_DEPTH`, default 64) answers `429 BACKPRESSURE` instead of growing.

`gpu_status()` (or `GET /v1/gpu`) reports `active_engine`, `loaded_models`,
`workers`, `vram_used_mb`, `vram_free_mb`, `queue_depth`, `in_flight`,
`unload_in_seconds`, `last_used_at`, and `cpu_engines[]` — the alive workers that
hold no VRAM, so `active_engine` can keep meaning "the GPU owner" while a
transcription is plainly visible. v0.6.0 adds `worker_devices` (`{engine: device}`),
`worker_vram_mb` (`{engine: mb | null}`) and `vram_source`
(`nvml` | `worker` | `none`), so a caller can tell a real 0 MB from an unknown one.
`server_info()` gains `device` and `devices` for the same reason.

## Concurrency and backpressure

Several agents — or one agent fanning out — can hit the daemon at once. The design
keeps that safe without letting a queue grow without bound.

- **Cache hits never wait.** A request the deterministic disk cache can answer is
  served immediately, even while another render holds the GPU.
- **Read-only calls never wait either.** `server_info`, `gpu_status` and
  `list_voices` never acquire the engine/GPU lock, so they stay responsive during
  a long synthesis.
- **Uncached jobs queue FIFO per engine.** They wait in arrival order instead of
  failing straight away with `WORKER_BUSY`, so a burst degrades into latency
  rather than errors.
- **`MAX_QUEUE_DEPTH` bounds that queue.** When it is full the request is refused
  with `429 BACKPRESSURE`, whose suggestion is to retry or to submit the work with
  `start_script_job`. A refusal costs a round-trip; an unbounded queue costs the
  machine.
- **`qwen3_tts_fast` is strictly serialized.** Its CUDA graphs are captured at
  batch 1, so two decodes cannot share the model and there is no micro-batching.
  Throughput comes from the cache and the queue. For long work,
  `start_script_job` keeps one call from occupying the engine slot while you poll.

Streaming has its own backpressure: the worker's PCM queue is bounded
(`STREAM_QUEUE_MAXSIZE`) and a consumer that stalls for
`STREAM_QUEUE_TIMEOUT_SECONDS` gets its stream cancelled with `BACKPRESSURE`
rather than buffering audio without limit.

## Choosing a backend when capture fails

`qwen3_tts_fast` captures CUDA graphs, which is fast but hardware-sensitive. If
capture fails on your box, install the official engine instead:

```bash
uv run agent-voice engines install qwen3_tts
uv run agent-voice models download qwen3_tts 1.7b-base
# then either set AGENT_VOICE_DEFAULT_ENGINE=qwen3_tts or pass engine="qwen3_tts"
```

It is slower per request and streams in an emulated way, but it is reliable. It is also
the only Qwen backend with a `cpu`, `mps` or `rocm` variant — `qwen3_tts_fast` is
CUDA-only — so a machine without an NVIDIA GPU uses `qwen3_tts` or the CPU engines. See
[troubleshooting.md](troubleshooting.md#cuda-graph-capture-fails-qwen3_tts_fast)
for the failure signature and the VRAM-free retry.
