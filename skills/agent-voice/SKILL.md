---
name: agent-voice
description: Synthesise speech and clone voices locally over MCP — use when a task needs audio narration, voice cloning, multi-speaker scripts, timed beats for video, or subtitles. Runs on the local GPU, returns file paths, never audio blobs.
---

# agent-voice

Local, GPU-backed TTS + voice cloning over MCP. Everything is disk-cached and
deterministic: repeating a request is nearly free.

## 1. Orient first — always

Call **`server_info()`** before anything else. It is free: it never spawns a worker
and never wakes the GPU. It tells you the version, default engine/voice,
`voices_count`, the registered voices, which engines are installed, and GPU state.

```
server_info() -> {version, default_engine, default_voice, voices_count, voices[],
                  installed_engines[], engines[], gpu_status{}}
```

Decisions to make from it:

- `voices_count == 0` → you must clone a voice before you can speak.
- `installed_engines` empty → no engine can run; report that, don't retry blindly.
- `engines[].why_unavailable` explains exactly why an engine can't be used.
- `gpu_status.loaded_models` non-empty → a model is already resident, so your first
  request is warm (fast); empty means a cold start is coming (tens of seconds).

## 2. First run — clone a voice

Only when `voices_count == 0` (or you genuinely need a new speaker):

```
clone_voice(
  name="narrator",
  ref_audio_path="/abs/path/clip.wav",   # 3-10 s of clean speech, one speaker
  ref_text="exact words spoken in the clip",
)
```

**`ref_text` matters.** With a transcript the engine runs in ICL mode and conditions
on both the audio *and* its words, which is audibly better than audio-only
(`x_vector_only=true`). It must match the clip word for word; a wrong transcript is
worse than none.

`clone_voice` is idempotent: same `name` + byte-identical audio returns the existing
voice. Different audio under an existing name errors — pass `overwrite=true` only
when you actually mean to replace it.

## 3. Then speak

**One utterance** — `speak(text="Hello world.")`.
Returns `file_path` (read it), `download_url`, `duration_sec`, `cached`,
`chunks_used`, `elapsed_ms`.

**Multi-beat narration** (podcast, video, explainer):

```
speak_script(
  beats=[
    {"text": "Intro line.", "pause_after_sec": 1.5},
    {"text": "Host question.", "voice": "host"},
    {"text": "Guest answer.", "voice": "guest", "target_duration_sec": 8.0,
     "fit_mode": "atempo"},
  ],
  export_srt=True,
)
```

Beat keys: `{text, voice?, engine?, model?, quality?, language?,
target_duration_sec?, fit_mode?, pause_after_sec?}`

- `pause_after_sec` (0–10) — silence after a beat, **included in the timings**, so
  visuals and subtitles stay frame-accurate. This is how you time a visual to land.
- per-beat `voice` — multi-speaker scripts. Each beat names a registered voice; the
  result's `beats[].voice_id` records who actually spoke.
- `target_duration_sec` + `fit_mode` (`none` | `pad` | `atempo`) — force a beat into a
  fixed slot for video. `pad` adds silence, `atempo` retimes speech.
- `export_srt=True` (default) also writes `.srt`, `.vtt` and a `.timing.json`.

Use `start_script_job(...)` instead for long scripts: it returns a `job_id` at once,
then poll `get_job(job_id)` until `status` is `succeeded` or `failed`.

## 4. Reproducibility

Pass `seed` (e.g. `seed=1234`) to `speak`/`speak_script` for identical audio from
identical inputs. Combined with the deterministic cache, a re-run is a cache hit and
returns the same artifact.

## 5. Cost and latency hygiene

- Everything is content-addressed on disk. **Repeating a request costs nothing** —
  don't pre-render out of fear, and don't worry about calling twice.
- VRAM is not released eagerly. After a large batch, call `unload_models()` to return
  the GPU to ~0 and leave the machine free for other work.
- If a request feels slow, call `gpu_status()`: a cold start (spawning the worker and
  loading weights) is legitimately tens of seconds; a warm request is not.
- Change only the variables you care about (voice, seed) — every distinct parameter
  combination is a separate cache entry.

## 6. Reading results

- `file_path` — absolute local path, readable directly. Prefer this.
- `download_url` — HTTP URL for the same artifact; hand it to another process instead
  of copying bytes yourself.
- `chunks_used > 1` — the text exceeded the chunk limit and was synthesised in
  pieces then stitched. **Normal, not an error.**
- `elapsed_ms` — wall-clock cost of the call. Near zero on a cache hit.
- `cached: true` — the disk cache answered; nothing touched the GPU.

## 7. Errors

| `error_code` | What it means | Do this |
|---|---|---|
| `VOICE_REQUIRED` | No voice exists or was selected; the engine cannot clone without one | `clone_voice(...)` first |
| `VOICE_NOT_FOUND` | Bad voice id/name | `list_voices()` and use a real id |
| `WORKER_BUSY` | Jobs are running / another engine holds the GPU | Retry shortly, or `unload_models(force=true)` |
| `TEXT_TOO_LONG` | Text over the per-call character limit | Split it, or use `speak_script` beats |
| `CUDA_OOM` | The worker ran out of GPU memory | `unload_models()`, or use the 0.6b model (`quality="fast"`) |
| `ENGINE_NOT_INSTALLED` | Engine present but its venv/deps are missing | `install_engine(engine_id)` (needs `ALLOW_AGENT_ENGINE_MUTATION=true`) |

Every error carries `message` and a concrete `suggestion`. Read the suggestion — it
usually names the exact next call.

## 8. Install

Command Code / Claude Code — MCP JSON (`~/.commandcode/mcp.json`, or the Claude Code
equivalent):

```json
{
  "mcpServers": {
    "agent-voice": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/agent-voice", "agent-voice", "mcp"],
      "env": {
        "AGENT_VOICE_CLIENT_ID": "command-code"
      }
    }
  }
}
```

`AGENT_VOICE_CLIENT_ID` becomes the `X-Agent-Id` header, which selects a per-agent
voice/engine binding (set one with `set_agent_binding`). The `mcp` subcommand is a
stdio shim: it starts the daemon if needed, then proxies MCP to it, so the agent
process never loads a model itself.

If the daemon already runs, skip the shim and point the client at
`http://127.0.0.1:8000/mcp/` (add `X-Agent-Id` yourself).
