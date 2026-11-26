# agent-voice

Headless, UI-free, multi-engine voice cloning and speech synthesis server built for AI agents.

An agent asks for speech over MCP or HTTP; `agent-voice` keeps the GPU idle until something
actually needs it, routes the request to an isolated engine subprocess, and returns a file
path or a PCM stream. No desktop app, no web UI, no base64 blobs in tool results.

## Why it exists

Most local TTS servers are built for humans: a window, a waveform, a "Generate" button.
Agents need something else — deterministic caching, honest error codes, a GPU that is
genuinely free when idle, and MCP tools that return compact JSON.

`agent-voice` is that server.

## Highlights

- **Single GPU owner.** The core daemon never imports torch. Engine code runs in isolated
  per-engine `uv` venvs, spawned lazily and killed on idle, so `nvidia-smi` shows 0 MB when
  nothing is being synthesised.
- **Pluggable engines.** Drop a directory into `engines/` with an `engine.yaml` and a
  `worker.py`; the registry discovers it. Two Qwen3-TTS backends ship today (official and
  CUDA-graph accelerated) with more documented in `engines/ADDING_AN_ENGINE.md`.
- **Deterministic cache.** SHA-256 over the full synthesis parameter set, checked *before* a
  worker is spawned, LRU-bounded.
- **Agent-native semantics.** `409 WORKER_BUSY` with a `force=true` suggestion, structured
  `{error_code, message, suggestion}` errors everywhere, force-unload that cancels in-flight
  jobs cleanly, and `MCP_BASE_PATH` confinement so returned paths stay readable inside a
  sandbox.
- **Real streaming and barge-in.** Length-prefixed multiplexed IPC over UDS or loopback TCP,
  with a dedicated worker reader thread so `cancel` lands while the GPU is mid-decode.

## Requirements

- Linux or Windows (macOS works for the mock engine; CUDA engines are Linux/Windows only)
- Python 3.12 for the core daemon
- `ffmpeg` and `ffprobe` on `PATH`
- `uv` for engine environment management
- For GPU engines: an NVIDIA GPU with a recent driver. Developed and verified on an
  RTX 4060 Ti 16 GB with driver 580.178.04.

## Install

```bash
git clone https://github.com/000jd/agent-voice.git agent-voice && cd agent-voice
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
cp .env.example .env
```

`uv sync --extra dev` works too and is what CI uses. Either way the core venv installs **no
torch** — that is deliberate, and it is what makes a 0 MB idle GPU a structural guarantee rather
than a promise.

The template defaults to `ALLOW_AGENT_ENGINE_MUTATION=false`; see [Security](#security) before you
change it.

## Quickstart with the mock engine

The mock engine synthesises tones, needs no models, and is the fastest way to verify the
plumbing end to end.

```bash
AGENT_VOICE_DEFAULT_ENGINE=mock uv run agent-voice serve
```

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/v1/audio/speech \
  -H 'content-type: application/json' \
  -d '{"model":"tts-1","input":"Hello from agent voice."}' --output out.wav
```

## Install a GPU engine

Two Qwen3-TTS backends ship, because they need **mutually exclusive** dependency
sets and cannot share a virtualenv:

| engine | package | streaming | when to use |
|---|---|---|---|
| `qwen3_tts_fast` | `faster-qwen3-tts` (CUDA graphs) | native | default; native streaming |
| `qwen3_tts` | `qwen-tts` (official) | emulated | fallback if graph capture misbehaves |

Honest throughput, measured on the reference box with the official backend and 0.6B: **~10.5 s of
wall time for 9.0 s of audio, warm — RTF ≈ 0.86**, i.e. slightly slower than real time. A cache hit
returns in ~0.01 s. That number was not measured on `qwen3_tts_fast`.

```bash
uv run agent-voice engines install qwen3_tts_fast
uv run agent-voice models download qwen3_tts_fast 0.6b-base
uv run agent-voice models download qwen3_tts_fast 1.7b-base
uv run agent-voice models list
```

Models land in the standard Hugging Face cache and are reused if already present.
`models list` reports `downloaded_locally` only when real weight files exist —
a bare `refs/` pointer is not treated as a usable model.

A voice is required for the Base checkpoints (they clone, they have no built-in
speakers):

```bash
uv run agent-voice voices add narrator ./my-clip.wav \
  --ref-text "exact transcript of the clip"
```

3–10 seconds of clean speech is the quality sweet spot.

### Measured on an RTX 4060 Ti 16 GB

| Metric | Value |
|---|---|
| Daemon boot with a GPU engine configured | **0 workers, 631 MiB VRAM** (= desktop idle) |
| Cold start (spawn + load 0.6B + 9.5 s speech) | 21.8 s |
| Warm request (model resident, 9.0 s speech) | 10.5 s |
| Warm real-time factor (9.0 s audio / 10.5 s wall) | **RTF ≈ 0.86** — slightly slower than real time |
| Cache hit (identical request) | 0.01 s |
| VRAM with 0.6B loaded | ~3.5 GB |
| VRAM after `unload(force=true)` | **639 MiB** — back to baseline |

Full numbers and dependency findings: [`docs/phase0-report.md`](docs/phase0-report.md).

## Connect an agent

`agent-voice mcp` is a stdio shim: it starts the daemon if it is not already running, then proxies
MCP to it, so the agent process never loads a model itself. Every snippet below uses the same
command — `uv run --directory <abs-path> agent-voice mcp` — with the absolute path to your checkout
substituted for `<abs-path>`.

### `.mcp.json` (project or user scope, portable `mcpServers` format)

Works for Command Code, Claude Code, Cursor, Copilot and anything else that reads a top-level
`mcpServers` object. Drop it at the repo root for a project-scoped server.

```json
{
  "mcpServers": {
    "agent-voice": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "<abs-path>", "agent-voice", "mcp"],
      "env": {
        "AGENT_VOICE_CLIENT_ID": "command-code"
      }
    }
  }
}
```

### Claude Desktop (`claude_desktop_config.json`)

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "agent-voice": {
      "command": "uv",
      "args": ["run", "--directory", "<abs-path>", "agent-voice", "mcp"],
      "env": {
        "AGENT_VOICE_CLIENT_ID": "claude-desktop"
      }
    }
  }
}
```

### VS Code

Put a top-level `servers` object in `.vscode/mcp.json` (workspace) or your profile's `mcp.json`.
Note the key is `servers`, not `mcpServers`, and the `type` is required:

```json
{
  "servers": {
    "agent-voice": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "<abs-path>", "agent-voice", "mcp"],
      "env": {
        "AGENT_VOICE_CLIENT_ID": "vscode"
      }
    }
  }
}
```

`AGENT_VOICE_CLIENT_ID` becomes the `X-Agent-Id` header, which selects a per-agent voice/engine
binding (`set_agent_binding`).

### HTTP MCP directly

When the daemon is already running, agents that speak Streamable HTTP can mount the endpoint
without a proxy:

```
http://127.0.0.1:8000/mcp/
```

Send `X-Agent-Id: <name>` to select a per-agent voice/engine binding.

> `X-Agent-Id` is convenience routing on a local single-user box, **not** an authorization
> boundary. If you need access control, set `AGENT_VOICE_API_KEY`.

### LangChain

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient({
    "agent-voice": {"transport": "http", "url": "http://127.0.0.1:8000/mcp/"}
})
tools = await client.get_tools()
```

## Tools

19 tools, split into a `core` and a `full` surface (`AGENT_VOICE_TOOLSET`, see
[Security](#security)). Every tool returns compact JSON — audio is never inlined as base64.

| Tool | Surface | Purpose |
|---|---|---|
| `server_info` | core | **Call first.** Version, defaults, voices, engines and GPU state in one call — never spawns a worker |
| `speak` | core | Synthesise one utterance, return a file path |
| `speak_script` | core | Multi-beat script with exact per-beat timings and SRT/VTT |
| `start_script_job` / `get_job` / `cancel_job` | core | Long scripts and other async work |
| `clone_voice` / `list_voices` | core | Manage reference voices |
| `gpu_status` / `unload_models` | core | Inspect and free the GPU |
| `delete_voice` | full | Remove a voice and its cached prompts |
| `set_agent_binding` / `list_agent_bindings` | full | Per-agent voice routing |
| `list_engines` / `list_models` | full | Discover engines, models, install state |
| `download_model` / `register_model` / `register_engine` / `install_engine` | full | Agent-driven self-management |

Failures come back as MCP `isError=true` with the JSON `{error_code, message, suggestion}` body as
the text, so a client can tell success from failure without parsing the payload.

The server also exposes `agent-voice://voices` (JSON) and `agent-voice://guide` (this repo's
SKILL.md), plus a `narrate_video` prompt.

### Orient with `server_info`

`server_info()` is free: it never spawns a worker and never wakes the GPU. It is the
right first call for any agent, because it answers the questions that otherwise cost
a failed synthesis round-trip:

```json
{
  "version": "0.1.0",
  "default_engine": "qwen3_tts_fast",
  "default_voice": "a1b2c3d4e5f6",
  "voices_count": 1,
  "voices": [{"voice_id": "a1b2c3d4e5f6", "name": "narrator", "duration_sec": 6.4}],
  "installed_engines": ["qwen3_tts_fast"],
  "engines": [{"engine_id": "qwen3_tts_fast", "installed": true, "streaming": true,
               "why_unavailable": null}],
  "gpu_status": {"active_engine": null, "loaded_models": [], "vram_used_mb": 631}
}
```

`voices_count == 0` means clone a voice before synthesising. `installed_engines`
tells you which engines can actually run, and `engines[].why_unavailable` explains
the ones that cannot.

### Return fields worth reading

`SynthesisResult` (from `speak`) carries more than a path:

| Field | Meaning |
|---|---|
| `file_path` | Absolute local path — read it directly |
| `download_url` | Relative HTTP URL for the same artifact, for handing to another process |
| `chunks_used` | How many text chunks were stitched. `>1` means the input exceeded `CHUNK_CHARS` and was split — normal, not an error |
| `elapsed_ms` | Wall-clock cost of the call; near zero on a cache hit, so latency regressions are obvious |
| `cached` | `true` when the deterministic disk cache answered and no GPU work happened |
| `real_time_factor` | Defined as `duration_sec / elapsed_sec` — above 1.0 means faster than real time, `None` when the duration is unknown. ⚠️ In the current build the daemon computes the reciprocal (`elapsed/duration`), so a render 37× faster than real time reports `0.027`; recompute from `duration_sec` and `elapsed_ms` if the value looks inverted. On the reference box a warm 0.6B render measured **≈ 0.86** (slightly slower than real time) |
| `take` | Which alternate delivery was rendered (`take=2`, …). A new take gets its own cache entry, so take 1 is untouched |

### Alternate takes of the same line

Want a different read of the same line? Pass `take=2` (or `3`, …) to `speak`, or `take` on an
individual `speak_script` beat. Each take is a separate cache entry, so asking again for the same
take is free and take 1 still resolves to the original audio. When you pass `take` without a
`seed`, the seed is derived from it, so a given take is reproducible.

### Idempotent voice cloning

`clone_voice` is safe to retry: the same `name` with byte-identical reference audio
returns the existing voice instead of creating a duplicate. If a voice with that name
already exists but the audio (or `ref_text`) differs, the call fails — silently
swapping a voice an agent is already using would be worse than erroring. Pass
`overwrite=true` (or `POST /v1/voices?overwrite=true`) to replace it deliberately.

## Scripts: beats, timings and multi-speaker

`speak_script` takes a list of beats. Every key is optional except `text`:

```python
speak_script(beats=[
    {"text": "Welcome back to the show.", "pause_after_sec": 1.5},
    {"text": "So, what changed this week?", "voice": "host"},
    {"text": "Everything, honestly.", "voice": "guest",
     "target_duration_sec": 8.0, "fit_mode": "atempo"},
], export_srt=True)
```

| Key | Effect |
|---|---|
| `text` | What to say (required) |
| `voice` | Voice id or name for **this beat** — enables multi-speaker scripts (podcasts, interviews); the result's `beats[].voice_id` records who actually spoke |
| `engine` / `model` / `quality` | Per-beat overrides of the same-named `speak` options |
| `language` | `"Auto"` or a language name |
| `target_duration_sec` + `fit_mode` | Fit the beat into a fixed slot (`none`, `pad`, `atempo`) — for fixed-length video segments |
| `pause_after_sec` | 0–10 s of silence inserted **after** the beat. Included in the reported timings, so subtitles and visuals stay frame-accurate |
| `take` | Alternate delivery of this beat's line (`take=2`, `take=3`, …). Re-record one beat without touching the others |

`BeatTiming` reports `start_sec`, `end_sec`, `pause_after_sec` and `voice_id` per
beat. `end_sec` is the end of the spoken portion; the next beat starts at
`end_sec + pause_after_sec`. That is what makes a Manim or slideshow timeline land
on the frame you expect.

`pause_after_sec` and per-beat `voice` need no MCP plumbing of their own — beats are
passed straight into `BeatSpec`, so anything the contract accepts works through the
tool.

### The `.timing.json` file

With `export_srt=true` (the default) a script render writes `<artifact>.timing.json` next to the
audio, alongside `.srt` and `.vtt`. It is the serialised `ScriptResult` — exactly this shape, no
extra keys:

```json
{
  "artifact_id": "a1b2c3d4e5f60718",
  "file_path": "/repo/data/artifacts/a1b2c3d4e5f60718.wav",
  "duration_sec": 8.9143,
  "beats": [
    {
      "index": 0,
      "text": "Welcome back to the show.",
      "duration_sec": 2.4123,
      "start_sec": 0.0,
      "end_sec": 2.4123,
      "pause_after_sec": 1.5,
      "target_duration_sec": null,
      "fit_mode": "none",
      "voice_id": "9f2c1d",
      "cached": false,
      "artifact_id": "0b1f..."
    },
    {
      "index": 1,
      "text": "Everything, honestly.",
      "duration_sec": 3.4012,
      "start_sec": 3.9123,
      "end_sec": 7.3135,
      "pause_after_sec": 0.0,
      "target_duration_sec": 8.0,
      "fit_mode": "atempo",
      "voice_id": "4a7e02",
      "cached": true,
      "artifact_id": "77cd..."
    }
  ],
  "srt_path": "/repo/data/artifacts/a1b2c3d4e5f60718.srt",
  "vtt_path": "/repo/data/artifacts/a1b2c3d4e5f60718.vtt",
  "timing_json_path": "/repo/data/artifacts/a1b2c3d4e5f60718.timing.json",
  "engine_id": "qwen3_tts_fast",
  "model_alias": "0.6b-base",
  "format": "wav"
}
```

| Field | Meaning |
|---|---|
| `artifact_id` / `file_path` / `duration_sec` | The stitched master and its true length |
| `beats[].index` | Position in the `beats` list you passed in — stable across re-renders |
| `beats[].text` | The beat's text, echoed back |
| `beats[].duration_sec` | Spoken length of this beat, **excluding** any trailing pause |
| `beats[].start_sec` / `end_sec` | Onset and end of the spoken portion. A following beat starts at `end_sec + pause_after_sec` |
| `beats[].pause_after_sec` | Silence inserted after this beat, included in the layout |
| `beats[].target_duration_sec` / `fit_mode` | The slot that was requested, echoed (`null` / `"none"` when unused) |
| `beats[].voice_id` | Which voice actually spoke this beat — auditable multi-speaker scripts |
| `beats[].cached` | `true` when this beat was reused from disk. Edit one beat of twelve and see eleven instant hits |
| `beats[].artifact_id` | This beat's own artifact, so a single beat can be fetched or re-rendered |
| `srt_path` / `vtt_path` / `timing_json_path` | Where the sidecars landed |

#### Consuming it from Manim

Read `start_sec` / `end_sec` instead of accumulating offsets yourself — every earlier
`pause_after_sec` is already baked into `start_sec`, which is what keeps a visual on the word it
belongs to:

```python
import json

from manim import FadeIn, FadeOut, Scene, Text

with open("a1b2c3d4e5f60718.timing.json", encoding="utf-8") as handle:
    timing = json.load(handle)

FADE_IN = 0.3
FADE_OUT = 0.1


class NarratedClip(Scene):
    def construct(self):
        # Manim's clock starts at 0, so the audio lines up with t=0.
        self.add_sound(timing["file_path"])
        beats = timing["beats"]
        # Scene time already rendered. Tracked by adding up what we ask for,
        # because animations consume scene time too.
        cursor = 0.0
        for position, beat in enumerate(beats):
            # start_sec already includes every earlier pause_after_sec, so hold
            # the screen until the beat is due rather than summing durations.
            gap = beat["start_sec"] - cursor
            if gap > 0:
                self.wait(gap)
                cursor += gap
            caption = Text(beat["text"]).scale(0.7)
            self.play(FadeIn(caption), run_time=FADE_IN)
            cursor += FADE_IN
            spoken = beat["end_sec"] - beat["start_sec"]
            remaining = spoken - FADE_IN
            if remaining > 0:
                self.wait(remaining)
                cursor += remaining
            # Fade out only if the pause leaves room. On back-to-back beats the
            # fade would push the next caption late, so cut instead.
            next_onset = (
                beats[position + 1]["start_sec"]
                if position + 1 < len(beats)
                else timing["duration_sec"]
            )
            if next_onset - beat["end_sec"] >= FADE_OUT:
                self.play(FadeOut(caption), run_time=FADE_OUT)
                cursor += FADE_OUT
            else:
                self.remove(caption)
```

```bash
# Manim does not mux narration into the render; attach it afterwards.
manim -qh --fps 30 scene.py NarratedClip
ffmpeg -y -i media/videos/scene/1080p30/NarratedClip.mp4 \
       -i a1b2c3d4e5f60718.wav \
       -c:v copy -c:a aac -shortest out.mp4
```

## Agent skill

An installable skill for coding agents lives at
[`skills/agent-voice/SKILL.md`](skills/agent-voice/SKILL.md) — orientation,
first-run voice cloning, script beats, reproducibility, error handling and the MCP
config snippet, in about 30 seconds of reading. The MCP server also serves it as the
`agent-voice://guide` resource.

## Security

The defaults are chosen so that a fresh `cp .env.example .env` is safe to run and safe to expose to
an agent.

- **Engine mutation is off by default.** `ALLOW_AGENT_ENGINE_MUTATION=false`. Enabling it lets an
  AI agent write `engine.yaml` / `worker.py` / `pyproject.toml` into `engines/` **and execute it** —
  `worker.py` is run as a Python script on this machine. That is arbitrary code execution requested
  by a language model. Leave it off unless you specifically want that.
- **Mutation is loopback-only.** Even with the flag on, both the daemon and the calling client must
  be on loopback. A non-loopback peer gets `AGENT_MUTATION_DISABLED`.
- **Loopback bind + Host/Origin guard.** The daemon binds `127.0.0.1` and rejects requests whose
  `Host` header is not a loopback name, which is what stops a DNS-rebinding page in your browser
  from talking to it. A present `Origin` must also be local. If you deliberately front the daemon
  with another hostname, add it to `AGENT_VOICE_ALLOWED_HOSTS`.
- **`/audio` is behind Bearer.** An artifact is the user's own speech (or a clone of someone
  else's), so `/audio/{artifact_id}` requires `Authorization: Bearer $AGENT_VOICE_API_KEY` when a
  key is set, exactly like `/v1/*` and `/mcp/*`. `/health` stays open.
- **`AGENT_VOICE_TOOLSET` limits what the agent can see.**

  | value | Tools exposed |
  |---|---|
  | `auto` (default) | `core`, plus the management tools when mutation is enabled |
  | `core` | Synthesis, voices read/clone, GPU status. No engine/model management at all |
  | `full` | Everything, so the per-call loopback + mutation guard answers with an actionable error |

  `core` is the right choice for an agent that only needs to make audio: a shorter tool list costs
  fewer tokens and has less for a model to get wrong.
- **`REQUIRE_ENGINE_APPROVAL`** stops agent-authored code from going live unattended. With
  `REQUIRE_ENGINE_APPROVAL=true`, `register_engine` stages the plugin under
  `engines/.pending/<id>/`; an operator inspects it and runs
  `agent-voice engines approve <id>` to move it into `engines/` and reload the registry.

### Voice cloning: consent is the operator's responsibility

Only clone voices you have permission to use. `agent-voice` runs locally and offline — no audio,
transcript or reference clip leaves this machine — but that is a property of the software, not a
licence. Cloning a real person's voice without their consent is impersonation, and in many
jurisdictions a legal problem, no matter where the model runs. The tool cannot verify consent for
you; the responsibility is the operator's.

## GPU lifecycle

- 0 workers at boot, 0 MB VRAM.
- Cache is consulted before any worker is spawned.
- Idle workers are reaped after `MODEL_IDLE_TTL_SECONDS` (default 900).
- When two engines are requested with `MAX_ACTIVE_ENGINES=1`, the active engine drains its
  queue first and residency hysteresis prevents ping-ponging.
- `unload_models(force=false)` returns `409 WORKER_BUSY` while jobs are running;
  `force=true` cancels them and frees VRAM immediately.
- With `ENABLE_VRAM_PRESSURE_YIELD=true`, an idle worker is killed when another application
  needs the GPU.

## Autostart

### Linux (systemd user unit)

```ini
# ~/.config/systemd/user/agent-voice.service
[Unit]
Description=agent-voice daemon
After=default.target

[Service]
Type=simple
WorkingDirectory=%h/projects/agent-voice
ExecStart=%h/projects/agent-voice/.venv/bin/agent-voice serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now agent-voice
```

### Windows (NSSM)

```powershell
nssm install agent-voice "C:\path\to\agent-voice\.venv\Scripts\agent-voice.exe" serve
nssm set agent-voice AppDirectory "C:\path\to\agent-voice"
nssm start agent-voice
```

Alternatively, Task Scheduler with trigger "At log on" and action
`agent-voice.exe serve` in the project directory.

## CLI

```
agent-voice serve | mcp | status
agent-voice engines  list | install <id> | test <id> | pending | approve <id>
agent-voice models   list | download <engine> <alias>
agent-voice voices   list | add <name> <wav> | delete <voice_id>
agent-voice bindings list | set <agent> ... | delete <agent>
agent-voice cache    stats | clear
agent-voice unload [--force]
```

### `engines test <engine_id>` — IPC v1 conformance

Spawns a real worker and walks the full protocol v1 contract, printing a pass/fail row per step.
Exits non-zero if anything fails, so it works as a CI or post-install gate. `mock` needs no GPU:

```console
$ agent-voice engines test mock
step           result  detail
-------------  ------  ---------------------------------------------------------------
spawn+connect  PASS    pid 473640, ready in 0.05 s
hello          PASS    protocol_version=1 engine_id=mock backend=mock 1 streaming=True
load_model     PASS    alias=mock-tone loaded_models=['mock-tone']
synthesize     PASS    1.92 s of audio in 0.27 s -> conformance.wav
cancel         PASS    cancel acknowledged, cancelled=True
shutdown       PASS    acknowledged, exited rc=0 in 0.05 s
mock: IPC v1 conformance passed
```

The steps are `spawn+connect`, `hello` (asserts `protocol_version == 1` and the engine id matches),
`load_model` (the registry's default alias), `synthesize` (asserts a WAV exists with duration > 0),
`cancel` (starts a stream, then cancels it; a cancel acknowledgement — including a `JOB_CANCELLED`
result — passes) and `shutdown`. Skipped steps are reported as `SKIP` with the reason, never as a
pass.

### `engines pending` / `engines approve <id>`

With `REQUIRE_ENGINE_APPROVAL=true`, agent-authored plugins land in `engines/.pending/<id>/`.
`engines pending` lists them; `engines approve <id>` validates the staged manifest, moves the
directory to `engines/<id>/`, and reloads the registry. It refuses to overwrite an existing engine
directory, and a manifest that cannot load blocks the move.

### `cache stats` / `cache clear`

`cache stats` prints entry count, total size, the configured cap and the oldest/newest entry as
JSON. It measures only — it never evicts. `cache clear` deletes every cached artifact and sidecar
and prints exactly what it removed and how many bytes it freed:

```console
$ agent-voice cache clear
{
  "cache_dir": "/repo/data/cache",
  "files_deleted": 6,
  "directories_removed": 3,
  "bytes_freed": 15407,
  "bytes_freed_human": "15.0 KiB",
  "deleted": [
    "92/921e...cea.json",
    "92/921e...cea.wav"
  ]
}
freed 15.0 KiB in 6 file(s)
```

## Adding an engine

See `engines/ADDING_AN_ENGINE.md`. In short: create `engines/<id>/engine.yaml`, a
`worker.py` that speaks IPC protocol v1, and optionally a `pyproject.toml` for an isolated
venv. No daemon changes required. Then prove it speaks the protocol:

```bash
uv run agent-voice engines test <engine_id>
```

## Development

```bash
uv sync --extra dev             # the dev extra supplies ruff + pytest; a bare
                                # `uv sync` installs neither

uv run ruff check .             # must stay clean
uv run pytest -v                # mock + contract suite, no GPU needed
PYTHONPATH=. .venv/bin/pytest -v   # if you use the console script directly:
                                   # conftest imports `tests._fake_clock`, so the
                                   # repo root has to be importable

AGENT_VOICE_RUN_GPU_TESTS=1 uv run pytest -v -m gpu -o addopts=""   # real GPU gate
uv run agent-voice engines test mock   # end-to-end IPC v1 conformance, ~1 s
```

CI runs exactly `uv sync --extra dev`, `ruff check .` and `pytest -v -m "not gpu"` on Python 3.12
against `ubuntu-latest`, with no GPU and no secrets — see `.github/workflows/ci.yml`.

Agent-facing orientation lives in [`AGENTS.md`](AGENTS.md).

## License

MIT. Architectural inspiration came from `debpalash/VoiceStudio` (AGPL-3.0); no code was
copied from it.
