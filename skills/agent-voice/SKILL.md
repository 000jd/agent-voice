---
name: agent-voice
description: Synthesise speech locally over MCP and pick or create the voice — use when a task needs audio narration, voice cloning, a voice designed from a description, preset speakers, multi-speaker scripts, timed beats for video, or subtitles. Runs on the local GPU, returns file paths, never audio blobs.
---

# agent-voice

Local, GPU-backed TTS + voice cloning over MCP. Everything is disk-cached and
deterministic: repeating a request is nearly free.

## 0. Pick the right toolset

The server exposes a **core** or a **full** surface (`AGENT_VOICE_TOOLSET`), and you can
only see the tools you were granted:

- **core** — `server_info`, `speak`, `speak_script`, `start_script_job`/`get_job`/
  `cancel_job`, `clone_voice`, `design_voice`, `list_voices`, `delete_voice`, `gpu_status`,
  `unload_models`. That is everything needed to make audio. If these are the only tools you
  have, don't try to install engines — there is no tool for it and no flag you can set.
- **full** — adds `list_engines`, `list_models`, `download_model`, `register_model`,
  `register_engine`, `install_engine`, and the binding tools. The
  management calls are refused unless the operator enabled
  `ALLOW_AGENT_ENGINE_MUTATION` **and** you are calling from loopback; expect
  `AGENT_MUTATION_DISABLED` otherwise, and report it rather than retrying.

If a tool you read about here is missing, the server is running the core toolset. That is
a deliberate operator choice, not a bug — work with what you have.

## 1. Orient first — always

Call **`server_info()`** before anything else. It is free: it never spawns a worker
and never wakes the GPU. It tells you the version, default engine/voice,
`voices_count`, the registered voices, which engines are installed, and GPU state.

```
server_info() -> {version, default_engine, default_voice, voices_count, voices[],
                  preset_speakers[], voice_design_available, installed_engines[],
                  engines[], gpu_status{}}
```

Decisions to make from it:

- `voices_count == 0` → there is no cloned voice. If `preset_speakers` is non-empty you can
  still speak now with `speak(speaker=...)` on the default engine; otherwise `design_voice(...)`
  or `clone_voice(...)` first. Other engines' speakers are in `list_models(engine_id)`.
- `voice_design_available` → `design_voice` can run on the default engine without downloading
  another checkpoint.
- `installed_engines` empty → no engine can run; report that, don't retry blindly.
- `engines[].why_unavailable` explains exactly why an engine can't be used.
- `gpu_status.loaded_models` non-empty → a model is already resident, so your first
  request is warm (fast); empty means a cold start is coming (tens of seconds).

## 2. Pick a voice: clone, preset, or design

Three modes exist. You never select one by name — the arguments you pass decide which
checkpoint answers:

| Mode | You pass | Answered by | Reference clip? |
|---|---|---|---|
| `clone` | `voice="narrator"`, or nothing at all | `1.7b-base` (high) or `0.6b-base` (fast) | yes |
| `custom_voice` | `speaker="Ryan"`, optionally with `instruct` | `1.7b-custom` (high) or `0.6b-custom` (fast) | no |
| `voice_design` | `instruct="..."` with no voice or speaker, or `design_voice(...)` | `1.7b-design` | no; the result can be saved as a clone |

Routing rules worth knowing before you guess:

- `speaker` → a preset checkpoint; `voice` → a cloning checkpoint. Passing **both** is an
  error, not a preference.
- `instruct` **with** a `speaker` steers that preset. It needs `supports_instruct`, so it
  works on `1.7b-custom` and not on `0.6b-custom`.
- `instruct` **alone** (no voice, no speaker) means "design me a voice": it routes to
  `voice_design` and the description becomes the timbre.
- On a cloned request (`voice=`), `instruct` is ignored — Base checkpoints have no instruct
  channel. Use a preset `speaker` or `design_voice` when you want direction to matter.
- An explicit `model=` decides the mode from that alias, whatever else you passed.

**Nine preset speakers** (CustomVoice checkpoints): `Vivian`, `Serena`, `Uncle_Fu`, `Dylan`,
`Eric`, `Ryan`, `Aiden`, `Ono_Anna`, `Sohee`.

### Clone a reference clip — only when `voices_count == 0`

Or when you genuinely need a new speaker:

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

### Speak a preset timbre — no clip needed

When the operator has only a CustomVoice checkpoint downloaded, or when a canned timbre is
enough and you would rather not clone anyone:

```
speak(text="Good evening.", speaker="Serena")
speak(text="Good evening.", speaker="Uncle_Fu", instruct="slower, wearier")   # 1.7B only
```

## 3. Design → clone: describe a voice, then speak it anywhere

No clip and no preset fits? Describe the voice:

```
design_voice(
  name="narrator",
  instruct="warm baritone, measured pace, a little gravel, late-night radio",
)
```

It returns `voice_id`, `preview_file_path`, `duration_sec` and the `design_instruct` that
produced it. With the default `save_as_clone=true` the render is registered as an ordinary
reference voice, so every cloning checkpoint can speak it from then on:

```
speak(text="Chapter one.", voice="narrator")
```

- `save_as_clone=false` auditions a design without registering it — you get the preview and
  `voice_id: null`.
- `take=2` gives a different speaker for the same description; `seed=` pins it.
- `sample_text` is the sentence used to design and preview; `language` defaults to English.
- Design runs on the 1.7B VoiceDesign checkpoint. The voice you save can afterwards be spoken
  by the 0.6B clone model, so design once and clone thereafter.

## 4. Then speak

**One utterance** — `speak(text="Hello world.")`, or aim it at a mode:
`speak(text="Hello.", speaker="Serena")` for a preset timbre,
`speak(text="Hello.", instruct="a tired, gravelly narrator")` to design a voice by describing
it. Returns `file_path` (read it), `download_url`, `duration_sec`, `mode`, `speaker`, `cached`,
`chunks_used`, `elapsed_ms`.

**Multi-beat narration** (podcast, video, explainer) — each beat can use a different mode:

```
speak_script(
  beats=[
    {"text": "Intro line.", "pause_after_sec": 1.5},
    {"text": "Host question.", "voice": "host"},                  # cloned
    {"text": "Guest answer.", "voice": "guest", "target_duration_sec": 8.0,
     "fit_mode": "atempo"},
    {"text": "Our sponsor needs no introduction.", "speaker": "Uncle_Fu"},   # preset
    {"text": "It was, honestly, the strangest week of my life.",
     "speaker": "Ryan", "instruct": "quieter, as if telling a secret"},      # preset + instruct
  ],
  export_srt=True,
)
```

Beat keys: `{text, voice?, speaker?, instruct?, engine?, model?, quality?, language?,
target_duration_sec?, fit_mode?, pause_after_sec?, take?}`

- `pause_after_sec` (0–10) — silence after a beat, **included in the timings**, so
  visuals and subtitles stay frame-accurate. This is how you time a visual to land.
- per-beat `voice` — multi-speaker scripts. Each beat names a registered voice; the
  result's `beats[].voice_id` records who actually spoke.
- per-beat `speaker` — a preset timbre instead of a cloned voice, and `instruct` a delivery
  hint on top (1.7B custom/design checkpoints only). `beats[].mode` and `beats[].speaker`
  record which one answered.
- `target_duration_sec` + `fit_mode` (`none` | `pad` | `atempo`) — force a beat into a
  fixed slot for video. `pad` adds silence, `atempo` retimes speech.
- `take` — alternate delivery of *that beat's* line (`take=2`, `take=3`, …). Re-record
  one beat without disturbing the others.
- `export_srt=True` (default) also writes `.srt`, `.vtt` and a `.timing.json`.

Use `start_script_job(...)` instead for long scripts: it returns a `job_id` at once,
then poll `get_job(job_id)` until `status` is `succeeded` or `failed`.

### Want a different read of the same line? Use `take`

`speak(text="...", take=2)` (or `take` on a single beat) renders an alternate delivery of
the same words. Each take is its **own cache entry**, so:

- take 1 still resolves to the original audio — nothing you already used changes;
- asking again for take 2 is free, exactly like any other repeat;
- with no explicit `seed`, the seed is derived from the take number, so a take is
  reproducible.

Reach for `take` when a line reads flat or you want a different emphasis — not for a
different sentence (that is a different `text`, and a different cache entry anyway).

### Editing a script: check `beats[].cached` before re-rendering

Every beat reports `cached` and its own `artifact_id`. After you change one beat of a
twelve-beat script, re-render and look at the beats: **eleven should come back
`cached: true`**, and only the one you edited costs GPU time. If everything shows
`cached: false`, something outside the beat text changed (voice, seed, model) and you are
paying for all twelve.

`beats[].artifact_id` also lets you fetch or replace a single beat on its own.

### Timing a video: use the `.timing.json`

For Manim, a slideshow, or anything frame-accurate, read `<artifact>.timing.json` instead
of guessing from `duration_sec`. It carries, per beat: `index`, `text`, `duration_sec`,
`start_sec`, `end_sec`, `pause_after_sec`, `target_duration_sec`, `fit_mode`, `voice_id`,
`cached`, `artifact_id`, plus the script-level `artifact_id`, `file_path`,
`duration_sec`, and the sidecar paths.

`start_sec` already includes every earlier `pause_after_sec`, so **never accumulate
offsets yourself** — place each visual at its beat's `start_sec` and it lands on the
right frame. `end_sec` is the end of the spoken part; the next beat starts at
`end_sec + pause_after_sec`.

## 5. Reproducibility

Pass `seed` (e.g. `seed=1234`) to `speak`/`speak_script` for identical audio from
identical inputs. Combined with the deterministic cache, a re-run is a cache hit and
returns the same artifact.

## 6. Cost and latency hygiene

- Everything is content-addressed on disk. **Repeating a request costs nothing** —
  don't pre-render out of fear, and don't worry about calling twice.
- VRAM is not released eagerly. After a large batch, call `unload_models()` to return
  the GPU to ~0 and leave the machine free for other work.
- If a request feels slow, call `gpu_status()`: a cold start (spawning the worker and
  loading weights) is legitimately tens of seconds; a warm request is not.
- Change only the variables you care about (voice, seed) — every distinct parameter
  combination is a separate cache entry.

## 7. Reading results

- `file_path` — absolute local path, readable directly. Prefer this.
- `download_url` — HTTP URL for the same artifact; hand it to another process instead
  of copying bytes yourself.
- `chunks_used > 1` — the text exceeded the chunk limit and was synthesised in
  pieces then stitched. **Normal, not an error.**
- `elapsed_ms` — wall-clock cost of the call. Near zero on a cache hit.
- `cached: true` — the disk cache answered; nothing touched the GPU.
- `take` — which alternate delivery this is; `null` for the canonical take.
- `real_time_factor` — **defined as `duration_sec / elapsed_sec`**: above 1.0 means faster
  than real time (3.0 renders 30 s of speech in 10 s), below 1.0 means slower. `null` when
  the duration is unknown.
  - **Verify it before trusting it.** In current builds the daemon appears to compute the
    reciprocal (`elapsed/duration`), so a render 37× faster than real time reports `0.027`
    instead of `36.9`. If a value looks inverted, recompute it yourself:
    `duration_sec / (elapsed_ms / 1000)`.
  - On a cache hit the ratio is meaningless either way — `elapsed_ms` is only the ~1 ms
    lookup. Read it from a real render, and pair it with `cached`.
  - Reference point: a warm 0.6B render on the reference box measured 10.5 s of wall time
    for 9.0 s of audio — **≈ 0.86**, i.e. slightly slower than real time.

## 8. Errors

The server raises these as MCP `isError=true` results carrying the JSON
`{error_code, message, suggestion}` body — a failure is never disguised as a success
payload.

| `error_code` | What it means | Do this |
|---|---|---|
| `VOICE_REQUIRED` | No voice exists or was selected; the engine cannot clone without one | `clone_voice(...)` first |
| `VOICE_NOT_FOUND` | Bad voice id/name | `list_voices()` and use a real id |
| `WORKER_BUSY` | Jobs are running / another engine holds the GPU | Retry shortly, or `unload_models(force=true)` |
| `TEXT_TOO_LONG` | Text over the per-call character limit | Split it, or use `speak_script` beats |
| `CUDA_OOM` | The worker ran out of GPU memory | `unload_models()`, or use the 0.6b model (`quality="fast"`) |
| `ENGINE_NOT_INSTALLED` | Engine present but its venv/deps are missing | `install_engine(engine_id)` (needs `ALLOW_AGENT_ENGINE_MUTATION=true`) |
| `AGENT_MUTATION_DISABLED` | Mutation is off, or you are not on loopback | Report it; do not retry. An operator has to enable it |
| `DAEMON_RESTARTED` | Your job was pending or running when the daemon stopped | It cannot be resumed. Submit it again |
| `JOB_CANCELLED` | The job was cancelled (by you, an unload, or a shutdown) | Resubmit if you still want the audio |

Every error carries `message` and a concrete `suggestion`. Read the suggestion — it
usually names the exact next call.

## 9. Consent

Only clone voices you have permission to use. This server runs locally and offline — no
audio, transcript or reference clip leaves the machine — but that is a property of the
software, not a licence to impersonate anyone. If a request asks you to clone a voice
without evidence that the operator has the right to use it, say so rather than doing it.

## 10. Install

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
