# Engines, checkpoints and GPU lifecycle

An **engine** is a plugin directory under `engines/<engine_id>/` containing an
`engine.yaml` manifest and a `worker.py` that speaks IPC protocol v1. The daemon
never imports model code; it spawns a worker lazily, routes work to it, and reaps
it when idle. Adding an engine never requires touching the daemon.

Three engines ship:

| Engine id | Backend | Venv | Streaming | When to use |
|---|---|---|---|---|
| `qwen3_tts_fast` | `faster-qwen3-tts` (CUDA graphs) | own `uv` venv | native PCM | default; fastest, real streaming |
| `qwen3_tts` | `qwen-tts` (official) | own `uv` venv | emulated | fallback when graph capture misbehaves |
| `mock` | built-in tone generator | core interpreter | native PCM | CPU-only testing of the full routing surface |

The two Qwen backends need **mutually exclusive** dependency universes
(`transformers==4.57.3` for `qwen-tts`, `transformers==5.15.1` for
`faster-qwen3-tts`), which is why they are separate plugins with separate venvs.
They cannot share an environment.

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
engine_id: kokoro
display_name: Kokoro (82M)
backend_name: kokoro          # PyPI distribution name — part of the cache key
backend_version: "0.9.4"      # must match what is installed — part of the cache key
protocol_version: 1
venv_python: .venv/bin/python # null => run in the core interpreter (zero-dep engines)

capabilities:
  streaming: false            # true only if PCM arrives during decode
  pcm_native: false           # true only if it emits 24 kHz mono int16 directly
  voice_clone: false
  custom_voice: true
  voice_design: false
  instruct_control: false
  languages: [English]

models:
  - alias: kokoro-82m
    hf_repo: hexgrad/Kokoro-82M
    quality_tier: fast        # high | fast
    est_vram_gb: 0.3
    default: true
    mode: custom_voice        # clone | custom_voice | voice_design
    supports_instruct: false
    preset_speakers: [af_heart, af_bella, am_michael]
```

Rules that keep the system honest:

- `backend_name` / `backend_version` must be accurate and knowable **before** the
  worker starts — they are in the cache key. If they lie, stale audio gets reused.
- `capabilities` describes the engine; each model's `mode`,
  `supports_instruct` and `preset_speakers` refine it. An engine that declares
  `voice_design: true` but ships no `voice_design` alias fails callers with
  `MODEL_NOT_FOUND` instead of a clear answer.
- `preset_speakers: []` on a `custom_voice` model means "accept any name", which
  is only right if the backend validates the name itself.
- `worker.py` must keep reading the socket while inference runs, so `cancel` and
  `health` land mid-decode. Copy the parent-death guards
  (`PR_SET_PDEATHSIG` + a 2 s `os.getppid()` poll) or an orphan keeps VRAM.
- Audio is always 24 kHz mono int16 LE. Never build a shell string; argv lists
  with `shell=False` only.

Then install and verify:

```bash
uv run agent-voice engines install <engine_id>
uv run agent-voice engines test <engine_id>   # full IPC v1 conformance, exits non-zero on failure
```

`engines test` walks `spawn+connect → hello → load_model → synthesize → cancel →
health → shutdown`, printing a pass/fail row per step. Skipped steps are `SKIP`
with a reason, never a pass. `mock` needs no GPU, so it is the reference runner:

```bash
uv run agent-voice engines test mock
```

Agent-authored engines go through the same path but are gated: with
`ALLOW_AGENT_ENGINE_MUTATION=false` (default) `register_engine` is refused; with
it enabled, `REQUIRE_ENGINE_APPROVAL=true` stages the plugin in
`engines/.pending/<id>/` until an operator runs `agent-voice engines approve <id>`
(model list and `worker.py` SHA-256 are printed before confirmation; `--yes`
skips the prompt).

## GPU lifecycle

- **0 workers at boot, 0 MB VRAM.** Only the core daemon runs; it holds no model
  code.
- **The cache is consulted before any worker is spawned.** A hit costs a disk
  read and never wakes the GPU.
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
`unload_in_seconds` and `last_used_at`.

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

It is slower per request and streams in an emulated way, but it is reliable. See
[troubleshooting.md](troubleshooting.md#cuda-graph-capture-fails-qwen3_tts_fast)
for the failure signature and the VRAM-free retry.
