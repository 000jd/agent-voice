# Configuration

All configuration is environment variables, read through `pydantic-settings`
(`src/agent_voice/config.py`). `.env.example` is the commented template; copy it
once and edit:

```bash
cp .env.example .env
```

## How settings are loaded

- Precedence: **process environment** > `.env` in the current working directory >
  code defaults. Names are case-insensitive; unknown variables are ignored.
- Settings are read once (cached) at startup. Editing `.env` requires a daemon
  restart. The one exception is the pronunciation lexicon — `data/pronunciations.json`,
  `data/pronunciations/<project>.json` and each voice's `pronunciations.json` — which
  is re-read on every request.
- A handful of values are validated at load: `NORMALIZE_LUFS` must be between
  `-40` and `-5`, `MAX_ACTIVE_ENGINES` between `1` and `2`, and all caps must be
  `>= 1`.
- `.env.example` ships `AGENT_VOICE_DEFAULT_ENGINE=qwen3_tts_fast`; the **code**
  default is `mock`, so a checkout with no `.env` at all starts on the CPU mock
  engine.

## Server

| Variable | Default | Effect |
|---|---|---|
| `AGENT_VOICE_HOST` | `127.0.0.1` | Bind address. A non-loopback value without an API key refuses to start. IPv6 literals (`::1`) work and are bracketed in generated URLs |
| `AGENT_VOICE_PORT` | `8000` | Bind port, and the base for every generated `download_url` |
| `AGENT_VOICE_API_KEY` | *(empty)* | When set, `/v1/*`, `/audio/*` and `/mcp/*` require `Authorization: Bearer <key>`; `/health`, `/docs`, `/redoc` and `/openapi.json` stay open. `/audio` artifacts need the header too, so a `download_url` is not a capability. WebSockets accept the same header or `Sec-WebSocket-Protocol: bearer, <key>`. A whitespace-only value counts as unset |

## Defaults

| Variable | Default | Effect |
|---|---|---|
| `AGENT_VOICE_DEFAULT_ENGINE` | `mock` in code, `qwen3_tts_fast` in `.env.example` | Engine used when a request names none |
| `AGENT_VOICE_DEFAULT_VOICE_ID` | *(empty)* | Voice used when a request names none. When empty and exactly one voice is registered, that voice is the default |

## GPU lifecycle

| Variable | Default | Effect |
|---|---|---|
| `MODEL_IDLE_TTL_SECONDS` | `900` | Idle seconds before a worker is reaped and VRAM returned |
| `MAX_ACTIVE_ENGINES` | `1` | How many engine venvs may be resident at once (`1`–`2`). At `1`, a request for another engine is `409 WORKER_BUSY` while the active engine still has queued work; at `2` the co-existence path is allowed |
| `MAX_RESIDENT_MODELS` | `1` | Models kept loaded per engine before LRU eviction |
| `MAX_GPU_VRAM_GB` | `12.5` | Declared VRAM budget for planning residency. It is recorded in settings and reported, but the current scheduler admits workers on measured free VRAM (`MIN_FREE_VRAM_GB`) rather than this number — do not rely on it to cap usage |
| `MIN_ENGINE_RESIDENCY_SECONDS` | `60` | Hysteresis: minimum residency before the scheduler may switch engines, which stops ping-ponging |
| `MIN_FREE_VRAM_GB` | `2.0` | VRAM pressure-yield threshold: when free VRAM drops below this and nothing is in flight, idle workers are killed so another GPU application can use the card |
| `ENABLE_VRAM_PRESSURE_YIELD` | `true` | Master switch for the pressure yield above; `false` keeps idle workers resident until `MODEL_IDLE_TTL_SECONDS` |
| `MAX_QUEUE_DEPTH` | `64` | Uncached jobs allowed to wait **across all engines** — a global admission cap, not a per-engine one. When it is reached, the request answers `429 BACKPRESSURE` instead of growing without bound |

Cache hits and read-only calls (`server_info`, `gpu_status`, `list_voices`) never
enter this queue; they are answered before an engine slot is requested.

## Cache and audio

| Variable | Default | Effect |
|---|---|---|
| `MAX_CACHE_GB` | `5.0` | LRU cap for `data/cache`. `cache stats` reports usage, `cache clear` empties it |
| `CHUNK_CHARS` | `400` | Long text is split at about this many characters and stitched. Not a rejection limit |
| `MAX_TEXT_CHARS` | `20000` | Cap on one utterance (`speak`, `/v1/audio/speech`, WS `speak`). Violation: `413 TEXT_TOO_LONG` |
| `MAX_SCRIPT_BEATS` | `200` | Cap on beats in one script. Violation: `400 BAD_REQUEST` |
| `MAX_INSTRUCT_CHARS` | `1000` | Cap on `instruct` for `speak`, a beat, or a voice design. Violation: `413 TEXT_TOO_LONG` |
| `MAX_DESIGN_SAMPLE_CHARS` | `500` | Cap on `design_voice(sample_text=…)`. Violation: `413 TEXT_TOO_LONG` |
| `MAX_REF_TEXT_CHARS` | `2000` | Cap on `clone_voice(ref_text=…)`. Violation: `413 TEXT_TOO_LONG` |
| `NORMALIZE_LUFS` | `-16.0` | Integrated loudness target for rendered audio; allowed range `-40` to `-5` |

`NORMALIZE_LUFS` applies to a **converted** single-utterance render (`mp3`, `opus`,
`flac`): ffmpeg runs one `loudnorm` pass after stitching. It deliberately does
**not** apply to:

- a `wav` render — the engine's (or stitched) bytes are copied byte-for-byte, so
  WAV stays bit-exact rather than loudness-normalised;
- a `pcm` response, which is never normalised;
- a script render (`speak_script` / `/v1/agent/script`), whose master is stitched
  from per-beat WAVs and converted without a loudnorm stage.

The value is part of the synthesis cache key, so changing it re-renders converted
output rather than serving a stale file.

The four input caps are enforced at admission — before routing, before a worker
is spawned and before any GPU time is spent. A rejected request creates no cache
entry and no scratch directory.

## Markup and lexicon limits

These are structural caps in code, not environment variables: they bound a
security boundary (an unbounded tag nest or an unbounded alternation is an
unbounded regex), so they are raised only by editing the contract deliberately.

| Limit | Value | Applies to |
|---|---|---|
| Markup tags | 128 | one utterance or beat |
| Markup nesting depth | 4 | `[slow][fast][emphasis][spell]` is already 4 |
| Markup segments | 32 | speech runs + pauses per utterance |
| `[pause]` | 0–10 s | one pause, `ms` or `s` |
| `paragraph_gap_ms` | 0–5000 | per request or beat |
| Lexicon entries | 500 | each scope (global, project, voice, per-call) |
| Lexicon entries, merged | 1,500 | global + project + voice + per-call |
| Lexicon key/replacement | 200 chars each | every scope |

Scopes live at `<DATA_DIR>/pronunciations.json` (global),
`<DATA_DIR>/pronunciations/<project>.json` (project, validated slug) and
`<DATA_DIR>/voices/<voice_id>/pronunciations.json` (voice). Precedence is global →
project → voice → per-call; see [api.md](api.md#pronunciation-lexicon-scopes-and-code-mode).

## Streaming

| Variable | Default | Effect |
|---|---|---|
| `STREAM_QUEUE_MAXSIZE` | `32` | Bounded PCM queue between worker and HTTP/WS consumer. Backpressure, not an unbounded buffer |
| `STREAM_QUEUE_TIMEOUT_SECONDS` | `5.0` | How long a consumer may stall before the stream is cancelled with `BACKPRESSURE` |

## Agent tooling and MCP

| Variable | Default | Effect |
|---|---|---|
| `ALLOW_AGENT_ENGINE_MUTATION` | `false` | Lets an MCP/REST caller write `engine.yaml` / `worker.py` / `pyproject.toml` into `engines/` **and execute it**. Arbitrary code execution requested by a language model — leave it off unless you want exactly that. Even when on, both daemon and caller must be on loopback |
| `REQUIRE_ENGINE_APPROVAL` | `true` | With mutation enabled, `register_engine` stages the plugin under `engines/.pending/<id>/` instead of activating it. An operator runs `agent-voice engines approve <id>` to inspect (model list + `worker.py` SHA-256), confirm, move and reload. `false` is the opt-out and means an agent's Python runs unread. Only meaningful when mutation is enabled |
| `AGENT_VOICE_TOOLSET` | `auto` | `auto`: core surface normally, full surface when mutation is enabled. `core`: synthesis + voices + GPU status only. `full`: everything, with the per-call loopback/mutation guard answering actionably |
| `MCP_BASE_PATH` | *(empty)* | Confines `ref_audio_path` reads to this directory **and** relocates artifacts to `<MCP_BASE_PATH>/artifacts`, so a returned `file_path` stays readable inside an agent sandbox |

## HTTP security

| Variable | Default | Effect |
|---|---|---|
| `AGENT_VOICE_ALLOWED_HOSTS` | `[]` | Extra `Host` header values accepted by the DNS-rebinding guard, **in addition to** `127.0.0.1`, `localhost`, `[::1]`/`::1`. Accepts `a,b` or `["a","b"]`. Add e.g. `devbox.lan:8000` only when you deliberately front the daemon with another hostname |
| `AGENT_VOICE_ALLOW_ANY_PATH` | `false` | `false` (safe default) confines **agent-supplied** `clone_voice` paths to `<DATA_DIR>/inbox/`. `true` allows any existing absolute local path. The CLI `agent-voice voices add` is an operator command and stays unrestricted either way. `MCP_BASE_PATH`, when set, takes precedence over this |

Rejected requests: foreign `Host` → `421`, foreign `Origin` → `403`, missing or
wrong bearer token → `401`, and a WebSocket that fails any check is refused before
`accept()` — over a real uvicorn that is an **HTTP `403`** on the handshake, not a
close frame. See [api.md](api.md#auth-and-hostorigin-rules).

## Paths

| Variable | Default | Effect |
|---|---|---|
| `DATA_DIR` | `./data` | Root for `voices/`, `inbox/`, `cache/`, `jobs/`, `artifacts/`, `pronunciations/`, `bindings.json`, `pronunciations.json`, `daemon.lock`, `daemon.pid`, `daemon.log`, `mutations.log`. Resolved to an absolute path at load |
| `ENGINES_DIR` | `./engines` | Where engine plugins are discovered. Resolved to an absolute path at load |

Relative values are resolved against the daemon's working directory at startup,
because workers are spawned with `cwd=<engine dir>` and a relative IPC socket path
would otherwise resolve somewhere else.

## Engine prerequisites

Some engines need a system package or a model that is not a pip dependency. These
are read-only checks in `agent-voice doctor`; none of them is an environment
variable.

| Prerequisite | Needed by | If missing |
|---|---|---|
| `ffmpeg` + `ffprobe` | every engine (reference canonicalisation, format conversion) | `FAIL` row — the daemon cannot produce non-WAV audio |
| `espeak-ng` | `kokoro` (English out-of-dictionary fallback, non-English G2P) | `WARN` row with the platform install command; common English still speaks |
| `en_core_web_sm` (spaCy) | `kokoro` English G2P | pinned as a direct dependency in `engines/kokoro/pyproject.toml`, so `engines install kokoro` brings it |
| CPU-only `torch` | `kokoro` | pinned to the `pytorch-cpu` index in the engine's `pyproject.toml`; a CUDA wheel there would defeat the zero-VRAM guarantee |
| NVIDIA driver + GPU | `qwen3_tts`, `qwen3_tts_fast` | `WARN` row ("mock/CPU only"); `kokoro` and `mock` still work |

## MCP stdio shim

| Variable | Default | Effect |
|---|---|---|
| `AGENT_VOICE_CLIENT_ID` | *(unset)* | When set in a server's `env` block, the stdio shim sends it as `X-Agent-Id`, selecting that agent's `set_agent_binding`. Convenience routing, not authentication |

## Tests

| Variable | Default | Effect |
|---|---|---|
| `AGENT_VOICE_RUN_GPU_TESTS` | `0` | Must be exactly `1` to run the `gpu` marker. CI pins it to `0` explicitly; the default `addopts` already excludes `gpu` |

## Set by the daemon for engine workers

Do not set these yourself; they are part of the engine IPC contract (see
[`engines/ADDING_AN_ENGINE.md`](../engines/ADDING_AN_ENGINE.md)):

| Variable | Meaning |
|---|---|
| `AGENT_VOICE_IPC_TRANSPORT` | `unix` (POSIX socket) or `tcp` (loopback, Windows) |
| `AGENT_VOICE_IPC_PATH` | Socket path when the transport is `unix` |
| `AGENT_VOICE_IPC_HOST` / `AGENT_VOICE_IPC_PORT` | Loopback address when the transport is `tcp` |
| `AGENT_VOICE_ENGINE_ID` | The engine id the worker serves |
| `AGENT_VOICE_ENGINE_MODELS` | JSON map of `alias -> hf_repo` for this engine |
| `AGENT_VOICE_DATA_DIR` | Absolute data directory |

## Changing a setting safely

1. Edit `.env`, not the host environment, so the value is reviewable in the repo.
2. Restart the daemon (`Ctrl-C` then `agent-voice serve`, or
   `systemctl --user restart agent-voice`).
3. Confirm with `agent-voice doctor` (environment, `ffmpeg`, the GPU, `espeak-ng`
   when Kokoro ships, engines, models, the port) and `agent-voice status` (health +
   GPU state). `server_info()` reports the effective `default_engine`,
   `default_voice`, `inbox_dir` and GPU snapshot from inside an agent.
4. For anything security-relevant (`ALLOW_AGENT_ENGINE_MUTATION`,
   `AGENT_VOICE_ALLOW_ANY_PATH`, `AGENT_VOICE_API_KEY`,
   `AGENT_VOICE_ALLOWED_HOSTS`), read [SECURITY.md](../SECURITY.md) first.
