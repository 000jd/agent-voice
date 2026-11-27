# Adding an engine to agent-voice

An engine is a directory under `engines/` containing a manifest and a worker
process. The daemon discovers it at boot, spawns it lazily, and talks to it over
a small multiplexed IPC protocol. **You never edit the daemon to add an engine.**

---

## 1. The contract

```text
engines/<engine_id>/
├── engine.yaml      # required — declarative manifest
├── worker.py        # required — speaks IPC protocol v1
└── pyproject.toml   # optional — only if the engine needs its own dependencies
```

### `engine.yaml`

```yaml
engine_id: kokoro                 # must equal the directory name, slug only
display_name: Kokoro (82M)
backend_name: kokoro              # PyPI distribution name, used in the cache key
backend_version: "0.9.4"          # the pinned version; must match what is installed
protocol_version: 1
venv_python: .venv/bin/python     # relative to this dir; null => core interpreter
kind: tts                         # tts speaks (synthesize), asr listens (transcribe); default tts
device: gpu                       # gpu competes for the single GPU slot; cpu never does; default gpu

capabilities:
  streaming: false                # true only if PCM arrives *during* decode
  pcm_native: false               # true only if it emits 24 kHz int16 directly
  voice_clone: false              # speaks a reference clip
  custom_voice: false             # speaks fixed, named timbres
  voice_design: false             # invents a timbre from a description
  instruct_control: false         # honours delivery/style instructions at all
  languages: [English]

models:
  - alias: kokoro-82m
    hf_repo: hexgrad/Kokoro-82M
    quality_tier: high            # high | fast
    est_vram_gb: 0.3
    default: true
    mode: clone                   # clone | custom_voice | voice_design
    supports_instruct: false      # this checkpoint honours `instruct`
    preset_speakers: []           # names `speaker=` accepts (custom_voice only)
```

Two fields are load-bearing:

- **`backend_name` / `backend_version`** are part of the synthesis cache key. They
  must be knowable *before* the worker starts, which is why they are declared
  here rather than discovered from `hello`. If they lie, stale audio gets reused.
- **`venv_python: null`** means "run me in the core daemon's interpreter". Only
  valid for engines with zero extra dependencies (see `engines/mock`).

Two more decide *scheduling*, not routing:

- **`kind`** — `tts` (default) or `asr`. It picks the IPC method the daemon calls
  (`synthesize`/`stream_pcm` versus `transcribe`) and the conformance table
  `engines test` walks. Defaulting to `tts` is what keeps every v0.4 manifest valid.
- **`device`** — `gpu` (default) or `cpu`. A `cpu` engine is **exempt from the
  single-GPU-owner slot**: a transcription no longer evicts a resident TTS model,
  and a CPU worker is never killed by the VRAM-pressure reaper. Declare it honestly;
  a `cpu` engine that then grabs a CUDA context wastes the promise, and a `gpu`
  engine that does not would hold the slot for nothing.

### `mode`: what each checkpoint can do

Every model declares exactly one mode, and the daemon routes on it. That is what makes
`voice=` / `speaker=` / `design_voice` reach a checkpoint that can actually honour them
instead of silently degrading:

| `mode` | Speaks | Reached by | Needs a reference clip |
|---|---|---|---|
| `clone` | a reference clip | `voice=<id or name>`, or nothing when a voice is registered | yes |
| `custom_voice` | fixed named timbres | `speaker="Ryan"` | no |
| `voice_design` | a timbre invented from words | `design_voice(instruct="...")` | no |

Two per-model fields refine that:

- **`supports_instruct`** — whether *this checkpoint* honours an `instruct` string. Qwen's
  1.7B CustomVoice and VoiceDesign checkpoints do; the 0.6B CustomVoice checkpoint speaks
  the same nine timbres but cannot take a delivery instruction. Declare it honestly: a
  caller that passes `instruct=` gets audio either way, so a `true` here that cannot be
  honoured produces silently wrong pacing, not an error.
- **`preset_speakers`** — the names `speaker=` accepts for a `custom_voice` model, e.g.
  `[Vivian, Serena, Uncle_Fu, Dylan, Eric, Ryan, Aiden, Ono_Anna, Sohee]`. The daemon matches
  case-insensitively, canonicalises to your spelling (the speaker is part of the cache key)
  and rejects anything else with `BAD_REQUEST`, so an unknown name never reaches your backend.
  Leave it empty for `clone` and `voice_design` models — and note that an empty list on a
  `custom_voice` model means "accept any name", which is only right if your backend validates
  it itself.

The engine-level `capabilities` flags (`voice_clone`, `custom_voice`, `voice_design`,
`instruct_control`) describe the *engine*, and are what `list_engines` and the daemon's
routing checks read before spawning anything. Keep them in step with the models you ship:
an engine that declares `voice_design: true` but ships no `mode: voice_design` alias fails
its callers with `MODEL_NOT_FOUND` instead of a clear "this engine cannot design voices".

### Honesty rules for `capabilities`

- `streaming: true` means audio is genuinely emitted while decoding. If you must
  synthesise fully then chunk the result, declare `streaming: false` and let the
  parent report `streaming_mode: "emulated"`.
- `pcm_native: true` means your chunks are already 24 kHz mono int16 LE. If your
  model emits 44.1 kHz, resample in the worker — the parent will not.
- `custom_voice: true` means `speaker=` works without a reference clip. If your backend
  needs a reference for every utterance, declare it `false` and ship only `clone` models.
- `voice_design: true` means a `synthesize` with `mode: "voice_design"` actually produces
  audio from a description. Do not declare it and then fall back to a random preset.
- `instruct_control: true` means an `instruct` string measurably changes the audio at
  least on one shipped checkpoint. Per-model `supports_instruct` is the finer answer; this
  flag is the engine-wide one.

---

## 2. The IPC protocol (v1)

One socket. Length-prefixed frames.

```text
frame := len_u32_be || type_u8 || payload
len_u32_be = bytes after the length field (1 + len(payload))
```

| type | payload |
|---|---|
| `0x01` JSON | UTF-8 JSON object, always with a unique `id` |
| `0x02` PCM | 16 raw UUID bytes (`stream_id`) + raw 24 kHz mono int16 LE |

### Why the dedicated reader thread matters

Your `worker.py` **must** keep reading the socket while inference runs. Otherwise
`cancel` and `health` sit unread behind a 20-second decode, and barge-in does not
work. The pattern every worker here uses:

```python
while not shutdown:
    frame_type, payload = conn.recv()      # reader thread
    if method in {"synthesize", "stream_pcm"}:
        threading.Thread(target=handler, args=(msg,), daemon=True).start()
    else:
        handler(msg)                       # hello/health/cancel answered inline
```

### Methods

| method | request | response |
|---|---|---|
| `hello` | `{id}` | `{protocol_version, engine_id, backend_name, backend_version, capabilities, loaded_models, gpu_vram_mb, pid}` (`+ kind: "asr"` on an ASR worker) |
| `load_model` | `{model_alias}` | `{model_alias, loaded_models}` |
| `unload_model` | `{model_alias}` | `{model_alias, loaded_models}` |
| `synthesize` | `{model_alias, text, ref_wav_path, ref_text, x_vector_only_mode, prompt_cache_path, wav_path, language, seed, speed, mode?, speaker?, instruct?}` | `{wav_path, sample_rate, duration_sec, model_alias, backend_name, backend_version}` |
| `stream_pcm` | same + `stream_id` | `PCM_CHUNK*`, then `{stream_id, done: true, duration_sec, streaming_mode}` |
| `transcribe` | `{model_alias, audio_path, language, word_timestamps, hotwords}` | `{model_alias, backend_name, backend_version, language, duration_sec, text, truncated, segments: [{index, start_sec, end_sec, text, words?}]}` |
| `cancel` | `{target_request_id}` | `{target_request_id, cancelled}` |
| `health` | `{id}` | `{ok, loaded_models, gpu_vram_mb, in_flight}` |
| `shutdown` | `{id}` | `{ok}` |

Errors are JSON replies: `{id, error_code, message, suggestion}`. Use
`CUDA_OOM` **only** when you actually caught an OOM — the parent trusts you.

Both families answer every method: a TTS worker replies `BAD_REQUEST unknown
method` to `transcribe`, and an ASR worker answers `synthesize`/`stream_pcm` the
same way. That is what keeps one dispatch table honest without a version bump.

### The optional `mode` / `speaker` / `instruct` fields

v0.3 added three fields to `synthesize` (and to `stream_pcm`, which carries the same
body). All three are **optional**: they default to `"clone"` / `null` / `""`, which is
exactly the v1 request. That is what keeps the contract frozen — an older worker that
ignores them still passes conformance, and `engines test` still sends no mode fields at
all, so read them with `msg.get(...)` rather than `msg[...]`.

| field | meaning | sent when |
|---|---|---|
| `mode` | which capability the parent routed to: `clone`, `custom_voice` or `voice_design` | always on a routed request; absent from a conformance probe |
| `speaker` | a preset timbre name, one of the model's `preset_speakers` | `mode == "custom_voice"` and the caller passed `speaker=` |
| `instruct` | natural-language delivery/style (custom voice) or the voice description (voice design) | whenever the caller supplied one; may be `""` |

Dispatch on `mode`, not on whether `ref_wav_path` happens to be empty:

```python
mode = msg.get("mode", "clone")
if mode == "clone":
    wavs = model.generate_voice_clone(ref_audio=..., ref_text=..., **kwargs)
elif mode == "custom_voice":
    wavs = model.generate_custom_voice(speaker=msg.get("speaker"), instruct=msg.get("instruct", ""), **kwargs)
elif mode == "voice_design":
    wavs = model.generate_voice_design(instruct=msg["instruct"], **kwargs)
else:
    reply({"error_code": "BAD_REQUEST", "message": f"unsupported mode: {mode}"})
```

Two rules keep this honest:

- **Never guess a speaker.** If `speaker` is not in this model's `preset_speakers`, reply
  with `BAD_REQUEST` and the accepted names rather than letting the backend pick.
- **Never pretend an instruction was applied.** If this checkpoint cannot take one
  (`supports_instruct: false`), say so with `BAD_REQUEST` and a suggestion naming an alias
  that can, instead of returning audio that quietly ignored it.

### Parent-death safety

Copy both guards. Without them an orphaned worker keeps VRAM allocated after the
daemon is killed.

```python
prctl(PR_SET_PDEATHSIG, SIGTERM)   # Linux, via ctypes
# plus a 2 s poll of os.getppid() on every platform
```

### Where to connect

The parent binds the socket **before** spawning you and passes:

```
AGENT_VOICE_IPC_TRANSPORT=unix   AGENT_VOICE_IPC_PATH=/…/engine.sock
# or
AGENT_VOICE_IPC_TRANSPORT=tcp    AGENT_VOICE_IPC_HOST=127.0.0.1  AGENT_VOICE_IPC_PORT=41234
```

Also provided: `AGENT_VOICE_ENGINE_MODELS` (JSON map of `alias -> hf_repo`) and
`AGENT_VOICE_DATA_DIR`.

---

## 3. Cancellation, done properly

Cooperative cancellation is the only honest kind.

- **Per-decode-step** (best): pass a `transformers.StoppingCriteria` that returns
  `cancel_event.is_set()`. See `engines/qwen3_tts/worker.py`.
- **Per-chunk** (good): check the event between yielded chunks, and call
  `generator.close()` so the decode loop unwinds. See
  `engines/qwen3_tts_fast/worker.py`.
- Never claim cancellation if you only check between whole utterances.

---

## 4. Install and verify

```bash
# create the isolated venv
uv sync --project engines/<engine_id>

# verify discovery + handshake
agent-voice engines list
agent-voice engines install <engine_id>

# smoke test
curl -s -X POST localhost:8000/v1/audio/speech \
  -H 'content-type: application/json' \
  -d '{"model":"<engine_id>:<alias>","input":"hello"}' -o out.wav
```

The daemon hot-reloads: `register_engine` scaffolds and reloads with no restart.

---

## 5. Worked examples

### Kokoro — a tiny, non-cloning engine

Fast, 82M parameters, fits alongside another engine in VRAM. No `ref_wav`. Kokoro's
built-in voices are a natural `custom_voice` engine, so that is what this declares:

```yaml
engine_id: kokoro
backend_name: kokoro
backend_version: "0.9.4"
venv_python: .venv/bin/python
capabilities:
  {streaming: false, voice_clone: false, custom_voice: true, instruct_control: false,
   languages: [English]}
models:
  - alias: kokoro-82m
    hf_repo: hexgrad/Kokoro-82M
    quality_tier: fast
    est_vram_gb: 0.3
    default: true
    mode: custom_voice
    preset_speakers: [af_heart, af_bella, am_michael]
```

`pyproject.toml`:

```toml
[project]
name = "agent-voice-engine-kokoro"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["kokoro>=0.9", "soundfile", "numpy", "torch"]
```

In `worker.py`, `synthesize` ignores `ref_wav_path`/`ref_text` and returns audio
at 24 kHz: `mode` is `custom_voice`, so the timbre comes from
`msg.get("speaker")`. Because `voice_clone: false`, the parent never sends a reference
for this engine — and `speaker=` is the only way to reach it.

### OmniVoice — cloning with a provider that needs its own model layout

```yaml
engine_id: omnivoice
backend_name: omnivoice
backend_version: "1.2.0"
capabilities: {streaming: true, pcm_native: true, voice_clone: true}
```

Checkpoint with the largest `est_vram_gb` you expect. If it exceeds
`MAX_GPU_VRAM_GB`, the registry will still list it — but the manager will refuse
to co-reside it with another engine.

### CosyVoice — reference-transcript dependent

```yaml
engine_id: cosyvoice
backend_name: cosyvoice
backend_version: "2.4.0"
capabilities: {streaming: true, voice_clone: true}
```

CosyVoice needs an accurate `ref_text`. In `worker.py`, if `x_vector_only_mode`
is true, reject with:

```python
conn.send_json({
    "id": msg["id"], "error_code": "BAD_REQUEST",
    "message": "cosyvoice requires ref_text (x_vector_only is unsupported)",
    "suggestion": "Transcribe the reference clip and pass it as ref_text.",
})
```

### Chatterbox — heavier model, VRAM-aware coexistence

```yaml
engine_id: chatterbox
backend_name: chatterbox-tts
backend_version: "0.1.4"
capabilities: {streaming: false, voice_clone: true}
```

Set `est_vram_gb` honestly. With `MAX_ACTIVE_ENGINES=2` the manager will admit it
only if both engines together fit under `MAX_GPU_VRAM_GB`; otherwise the
anti-thrash scheduler keeps them sequential.

---

## 6. Adding an ASR engine

An ASR engine is the same plugin shape with `kind: asr`. It listens instead of
speaking: the daemon calls `transcribe`, never `synthesize`/`stream_pcm`, and
`engines test` walks a different fourth step.

```yaml
kind: asr
device: cpu
```

- **`kind: asr`** routes the whole family: `resolve_asr_target` picks it,
  `engines test` uses the `transcribe` table, and a `synthesize` request aimed at
  it is refused with a suggestion naming the TTS engines rather than a crash.
- **`device: cpu`** is what makes the zero-VRAM claim structural. A `cpu` engine
  **never takes the GPU owner slot**: `_begin_job` skips `_acquire_gpu` for it, so
  a transcription runs while a TTS model is resident instead of evicting it, and
  the VRAM-pressure reaper leaves it alone. `phonon` and `kokoro` both declare
  `cpu`; `qwen3_tts*` and `mock` are the `gpu`/default cases.

Declare both honestly and nothing in the core daemon needs editing. Two ASR
engines ship today: `engines/phonon/` (real, `device: cpu`) and
`engines/mock_asr/` (dependency-free, the reference for the shape).

### The `transcribe` request and response

Copied from `src/agent_voice/ipc/protocol.py`, which is the frozen contract:

```text
transcribe   req  {"id": str, "method": "transcribe", "model_alias": str,
                   "audio_path": str, "language": str|None,
                   "word_timestamps": bool, "hotwords": [str]}
             resp {"id": str, "model_alias": str, "backend_name": str,
                   "backend_version": str, "language": str,
                   "duration_sec": float, "text": str, "truncated": bool,
                   "segments": [{"index": int, "start_sec": float,
                                 "end_sec": float, "text": str,
                                 "words": [{"word": str, "start_sec": float,
                                            "end_sec": float}]}]}
```

- `audio_path` is a **16 kHz mono WAV** the parent already extracted with ffmpeg.
  Do not assume the original container, and do not shell out to decode it: the
  expensive conversion has already happened, once, outside the worker.
- **`word_timestamps`** — populate each segment's `words` **only** when it is
  true. A caller that did not ask still gets valid audio-level timing from
  `start_sec`/`end_sec`; making it pay for per-word decode is wasted work.
- **`hotwords`** — a decoder *bias* list, capped at **25** entries
  (`MAX_HOTWORDS`) with each term at most 64 characters, validated by the parent
  before the request is sent. Bias only: a term that is not in the audio must not
  appear in the transcript, or the feature is fabrication rather than recognition.
- `language` — `null` means "detect it". A fixed-language checkpoint should
  ignore the hint (and say what it is) rather than attempt a language it cannot
  accept. Report the language you actually used, or `"auto"` when the backend
  gives you nothing; never invent one.
- `segments` **may be `[]`** for silence-only audio, and `truncated: true` means
  the decode window ran out of token budget — say so rather than silently
  returning half a transcript.

### Segment grouping: the three rules

The parent relies on one specific shape, so state it and follow it. A segment
closes at whichever comes **first**:

1. **Sentence punctuation** — `.`, `!`, `?` (and the equivalent closing quote or
   bracket after it).
2. **A pause `>= 0.8 s`** between consecutive words (`next.start - prev.end`).
3. **A 7 s cap** — a long unpunctuated run is still split, so no single segment
   becomes unreadable in a subtitle track or unusable as a dubbing slot.

`engines/phonon/worker.py::group_words_into_segments` is the reference
implementation of exactly these three rules; copy its logic (or import the idea)
rather than inventing a fourth rule. If the backend hands you its own `segments`,
you may use them — but they must honour the same three properties, because
subtitles and dubbing both assume a segment is a natural, bounded span.

### Worked example: `engines/faster_whisper/`

A real CPU ASR engine needs three files and **zero edits** to the core daemon —
no registry change, no manager change, no route change.

```text
engines/faster_whisper/
├── engine.yaml
├── pyproject.toml
└── worker.py
```

`engine.yaml`:

```yaml
engine_id: faster_whisper
display_name: Faster-Whisper (CTranslate2, CPU)
backend_name: faster-whisper
backend_version: "1.0.3"
protocol_version: 1
venv_python: .venv/bin/python
kind: asr
device: cpu
capabilities:
  {streaming: false, pcm_native: false, voice_clone: false,
   word_timestamps: true, hotwords: true, multilingual: true,
   languages: [en, es, fr, de, it, pt, nl, ru, zh, ja]}
models:
  - {alias: whisper-small,  hf_repo: Systran/faster-whisper-small,
     quality_tier: fast, est_vram_gb: 0.0}
  - {alias: whisper-medium, hf_repo: Systran/faster-whisper-medium,
     quality_tier: high, est_vram_gb: 0.0, default: true}
```

Two aliases, one per quality tier: the router picks `high` unless a caller asks
for `quality="fast"`, and exactly one carries `default: true`.

`pyproject.toml` — the CPU-torch index pattern, so the engine can never see a
GPU device even on a GPU box:

```toml
[project]
name = "agent-voice-engine-faster-whisper"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "faster-whisper==1.0.3",
  "torch==2.15.0",          # ctranslate2 needs the runtime; pin it directly
  "numpy>=1.26",
  "soundfile>=0.12",
]

[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true

[tool.uv.sources]
torch = { index = "pytorch-cpu" }

[tool.uv]
package = false
```

`torch` is a **direct** dependency with an explicit CPU index on purpose: as a
transitive dependency it resolves the CUDA wheel, drags in every `nvidia-*`
package and Triton, and the "0 MB VRAM" promise becomes a hope. See
[`docs/phase0-asr-report.md`](../docs/phase0-asr-report.md) for the measured
version of this on Phonon.

`worker.py`, the load and transcribe handlers (the rest is the reader thread,
parent-death guards and dispatch table from `engines/mock/worker.py`):

```python
import os
os.environ["CUDA_VISIBLE_DEVICES"] = ""      # before any CUDA-capable import

_models: dict[str, object] = {}

def handle_load(msg):
    from faster_whisper import WhisperModel       # heavy import inside the handler
    alias = msg["model_alias"]
    if alias not in _models:
        _models[alias] = WhisperModel(alias, device="cpu", compute_type="int8")
    reply({"id": msg["id"], "model_alias": alias, "loaded_models": sorted(_models)})

def handle_transcribe(msg):
    model = _models[msg["model_alias"]]
    segments, info = model.transcribe(
        msg["audio_path"],
        language=msg.get("language") or None,
        word_timestamps=bool(msg.get("word_timestamps")),
        hotwords=msg.get("hotwords") or None,
        vad_filter=True,
    )
    out = [
        {"index": i, "start_sec": seg.start, "end_sec": seg.end, "text": seg.text.strip(),
         "words": [{"word": w.word, "start_sec": w.start, "end_sec": w.end}
                   for w in (seg.words or [])]}          # only when asked for
        for i, seg in enumerate(segments)
    ]
    reply({"id": msg["id"], "model_alias": msg["model_alias"], "backend_name": "faster-whisper",
           "backend_version": "1.0.3", "language": info.language or "auto",
           "duration_sec": info.duration, "text": " ".join(s["text"] for s in out),
           "truncated": False, "segments": out})
```

**Zero core daemon edits.** Discovery, install, routing, the REST/MCP surfaces,
dubbing, the idle reaper and `engines test` all work from the manifest alone.

### ASR checklist rows

In addition to the general checklist in §7:

- [ ] `kind: asr` in `engine.yaml`, and `device` declared honestly (`cpu` for a
      CPU engine, so it is exempt from the GPU slot)
- [ ] `hello` reports `"kind": "asr"` and accurate `capabilities`
      (`word_timestamps`, `hotwords`, `multilingual`, `languages`)
- [ ] Word timings are returned **only** when `word_timestamps` is true
- [ ] `hotwords` is a bias list, capped at 25, and never fabricates words
- [ ] Segments group on sentence punctuation, a `>= 0.8 s` pause and a 7 s cap
- [ ] `language` reports what was used, or `"auto"` — never a guess
- [ ] `cancel` is honoured mid-decode, and `{cancelled: false}` when it lost the race
- [ ] `agent-voice engines test <id>` reaches 7/7 on the `transcribe` table

---

## 7. Checklist

- [ ] Directory name equals `engine_id`, both are slugs
- [ ] `backend_version` matches the pinned distribution exactly
- [ ] `kind` and `device` are declared (`kind: asr` for a transcription engine,
      `device: cpu` for one that claims no VRAM)
- [ ] `worker.py` reads a dedicated thread; `synthesize` runs off it
- [ ] Parent-death guards present (`PR_SET_PDEATHSIG` + ppid poll)
- [ ] Cancel is honoured mid-generation, not only between requests
- [ ] All audio normalised to 24 kHz mono int16 LE
- [ ] `hello` reports accurate `capabilities` and `gpu_vram_mb`
- [ ] Every model declares the right `mode`; `preset_speakers` matches what the backend
      really accepts; `supports_instruct` is only `true` where `instruct` changes the audio
- [ ] `mode` / `speaker` / `instruct` are read with `msg.get(...)`, so a probe without them
      still synthesises
- [ ] No `shell=True`, no string-built commands
- [ ] `agent-voice engines install <id>` reaches a clean `hello`
- [ ] `agent-voice engines test <id>` passes, and `voices design` works if
      `voice_design: true`
