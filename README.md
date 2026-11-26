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
git clone <your-fork> agent-voice && cd agent-voice
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
cp .env.example .env
```

The core venv installs **no torch**. That is deliberate — it is what makes a 0 MB idle GPU
a structural guarantee rather than a promise.

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
| `qwen3_tts_fast` | `faster-qwen3-tts` (CUDA graphs) | native | default; fastest, real-time PCM |
| `qwen3_tts` | `qwen-tts` (official) | emulated | fallback if graph capture misbehaves |

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
| Cache hit (identical request) | 0.01 s |
| VRAM with 0.6B loaded | ~3.5 GB |
| VRAM after `unload(force=true)` | **639 MiB** — back to baseline |

Full numbers and dependency findings: [`docs/phase0-report.md`](docs/phase0-report.md).

## Connect an agent

### Command Code / Claude Code / Cursor

The `agent-voice mcp` command is a stdio shim. It starts the daemon if it is not running,
then proxies MCP to it — so the agent never loads a model itself.

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

| Tool | Purpose |
|---|---|
| `server_info` | **Call first.** Version, defaults, voices, engines and GPU state in one call — never spawns a worker |
| `speak` | Synthesise one utterance, return a file path |
| `speak_script` | Multi-beat script with exact per-beat timings and SRT/VTT |
| `start_script_job` / `get_job` / `cancel_job` | Long scripts and other async work |
| `clone_voice` / `list_voices` / `delete_voice` | Manage reference voices |
| `set_agent_binding` / `list_agent_bindings` | Per-agent voice routing |
| `list_engines` / `list_models` | Discover engines, models, install state |
| `download_model` / `register_model` / `register_engine` / `install_engine` | Agent-driven self-management |
| `gpu_status` / `unload_models` | Inspect and free the GPU |

Every tool returns compact JSON. Audio is never inlined as base64.

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

`BeatTiming` reports `start_sec`, `end_sec`, `pause_after_sec` and `voice_id` per
beat. `end_sec` is the end of the spoken portion; the next beat starts at
`end_sec + pause_after_sec`. That is what makes a Manim or slideshow timeline land
on the frame you expect.

`pause_after_sec` and per-beat `voice` need no MCP plumbing of their own — beats are
passed straight into `BeatSpec`, so anything the contract accepts works through the
tool.

## Agent skill

An installable skill for coding agents lives at
[`skills/agent-voice/SKILL.md`](skills/agent-voice/SKILL.md) — orientation,
first-run voice cloning, script beats, reproducibility, error handling and the MCP
config snippet, in about 30 seconds of reading.

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
agent-voice engines  list | install <id>
agent-voice models   list | download <engine> <alias>
agent-voice voices   list | add <name> <wav> | delete <voice_id>
agent-voice bindings list | set <agent> ... | delete <agent>
agent-voice unload [--force]
```

## Adding an engine

See `engines/ADDING_AN_ENGINE.md`. In short: create `engines/<id>/engine.yaml`, a
`worker.py` that speaks IPC protocol v1, and optionally a `pyproject.toml` for an isolated
venv. No daemon changes required.

## Development

```bash
uv run pytest -v                 # mock + contract suite, no GPU needed
AGENT_VOICE_RUN_GPU_TESTS=1 uv run pytest -v -m gpu   # real GPU gate
```

## License

MIT. Architectural inspiration came from `debpalash/VoiceStudio` (AGPL-3.0); no code was
copied from it.
