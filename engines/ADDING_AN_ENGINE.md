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

capabilities:
  streaming: false                # true only if PCM arrives *during* decode
  pcm_native: false               # true only if it emits 24 kHz int16 directly
  voice_clone: false
  languages: [English]

models:
  - alias: kokoro-82m
    hf_repo: hexgrad/Kokoro-82M
    quality_tier: high            # high | fast
    est_vram_gb: 0.3
    default: true
```

Two fields are load-bearing:

- **`backend_name` / `backend_version`** are part of the synthesis cache key. They
  must be knowable *before* the worker starts, which is why they are declared
  here rather than discovered from `hello`. If they lie, stale audio gets reused.
- **`venv_python: null`** means "run me in the core daemon's interpreter". Only
  valid for engines with zero extra dependencies (see `engines/mock`).

### Honesty rules for `capabilities`

- `streaming: true` means audio is genuinely emitted while decoding. If you must
  synthesise fully then chunk the result, declare `streaming: false` and let the
  parent report `streaming_mode: "emulated"`.
- `pcm_native: true` means your chunks are already 24 kHz mono int16 LE. If your
  model emits 44.1 kHz, resample in the worker — the parent will not.

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
| `hello` | `{id}` | `{protocol_version, engine_id, backend_name, backend_version, capabilities, loaded_models, gpu_vram_mb, pid}` |
| `load_model` | `{model_alias}` | `{model_alias, loaded_models}` |
| `unload_model` | `{model_alias}` | `{model_alias, loaded_models}` |
| `synthesize` | `{model_alias, text, ref_wav_path, ref_text, x_vector_only_mode, prompt_cache_path, wav_path, language, seed, speed}` | `{wav_path, sample_rate, duration_sec, model_alias, backend_name, backend_version}` |
| `stream_pcm` | same + `stream_id` | `PCM_CHUNK*`, then `{stream_id, done: true, duration_sec, streaming_mode}` |
| `cancel` | `{target_request_id}` | `{target_request_id, cancelled}` |
| `health` | `{id}` | `{ok, loaded_models, gpu_vram_mb, in_flight}` |
| `shutdown` | `{id}` | `{ok}` |

Errors are JSON replies: `{id, error_code, message, suggestion}`. Use
`CUDA_OOM` **only** when you actually caught an OOM — the parent trusts you.

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

Fast, 82M parameters, fits alongside another engine in VRAM. No `ref_wav`.

```yaml
engine_id: kokoro
backend_name: kokoro
backend_version: "0.9.4"
venv_python: .venv/bin/python
capabilities: {streaming: false, voice_clone: false, languages: [English]}
models:
  - {alias: kokoro-82m, hf_repo: hexgrad/Kokoro-82M, quality_tier: fast, est_vram_gb: 0.3, default: true}
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
at 24 kHz. Because `voice_clone: false`, the parent never sends a reference.

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

## 6. Checklist

- [ ] Directory name equals `engine_id`, both are slugs
- [ ] `backend_version` matches the pinned distribution exactly
- [ ] `worker.py` reads a dedicated thread; `synthesize` runs off it
- [ ] Parent-death guards present (`PR_SET_PDEATHSIG` + ppid poll)
- [ ] Cancel is honoured mid-generation, not only between requests
- [ ] All audio normalised to 24 kHz mono int16 LE
- [ ] `hello` reports accurate `capabilities` and `gpu_vram_mb`
- [ ] No `shell=True`, no string-built commands
- [ ] `agent-voice engines install <id>` reaches a clean `hello`
