# API reference

`agent-voice` speaks four interfaces over one loopback socket:

- **REST** — `/v1/*` JSON endpoints and `/audio/{artifact_id}` artifacts.
- **OpenAI-compatible** — `POST /v1/audio/speech`, drop-in for an OpenAI SDK client.
- **Streaming** — chunked PCM over `POST /v1/audio/speech/pcm-stream`, and a
  barge-in WebSocket at `/v1/audio/ws`.
- **MCP** — Streamable HTTP mounted at `/mcp/`, plus a stdio shim
  (`agent-voice mcp`) for clients that cannot speak HTTP.

The wire contract lives in `src/agent_voice/schemas.py` and is treated as frozen:
fields are added, never silently repurposed. This document describes v0.3.3.

Default base URL: `http://127.0.0.1:8000`. Interactive schema: `/docs` and
`/openapi.json`.

## Auth and Host/Origin rules

A pure ASGI guard (`src/agent_voice/api/guard.py`) runs for **both** HTTP and
WebSocket scopes, before routing and before a WebSocket `accept()`. The same
rules apply to every route, including the mounted `/mcp` app.

| Check | Rule | Failure |
|---|---|---|
| `Host` | Must canonicalise to `127.0.0.1`, `localhost`, `[::1]`/`::1`, or a name in `AGENT_VOICE_ALLOWED_HOSTS` | `421` with `{"error_code":"BAD_REQUEST","message":"host not allowed: …"}` |
| `Origin` | If the header is present, its host must be in the same allowed set (an opaque `null` origin is rejected) | `403` |
| `Authorization` | Required when `AGENT_VOICE_API_KEY` is set, for `/v1/*`, `/audio/*` and `/mcp/*` | `401` |
| Exempt paths | `/health`, `/docs`, `/openapi.json` (and anything not under `/v1`, `/audio`, `/mcp`) | — |

Notes:

- For a WebSocket, a failed check is not an HTTP status: the socket is closed
  with code **`1008`** before `accept()` (see
  [WebSocket](#websocket-v1audiows)).
- `AGENT_VOICE_ALLOWED_HOSTS` **extends** the loopback defaults; it never replaces
  them. Both `a,b` and `["a","b"]` forms are accepted.
- The guard exists to stop a DNS-rebinding page in your browser from reaching the
  daemon. It is not a substitute for an API key if you expose the port.
- A non-loopback bind without `AGENT_VOICE_API_KEY` refuses to start; the daemon
  will not be an unauthenticated TTS/clone relay on your LAN.
- **WebSocket credential.** Browsers cannot set `Authorization` on a WebSocket, so
  the accepted alternative is the subprotocol header:

  ```
  Sec-WebSocket-Protocol: bearer, <key>
  ```

  A plain `Authorization: Bearer <key>` also works. A handshake that fails Host,
  Origin or auth is closed with code **`1008`** (RFC 6455 policy violation)
  *before* `accept()`, so no audio ever flows. When the token arrives in the
  subprotocol list the server selects and echoes the `bearer` subprotocol.

`X-Agent-Id: <name>` selects a per-agent voice/engine binding. It is convenience
routing on a local single-user box, **not** an authorization boundary.

## Error envelope

Every failure — REST, MCP tool result, IPC reply — uses one body:

```json
{
  "error_code": "TEXT_TOO_LONG",
  "message": "text is 25000 characters, limit is 20000",
  "suggestion": "Split into several calls or use speak_script for long content."
}
```

`error_code` is an enum shared across transports:

| Code | HTTP | Meaning |
|---|---|---|
| `WORKER_BUSY` | 409 | An operation needs the engine exclusively while work is running: `unload` without force, an engine switch while the active engine drains, or a residency window |
| `WORKER_CRASHED` | 500 | The worker died; the manager will respawn it |
| `CUDA_OOM` | 503 | The engine caught a real CUDA out-of-memory |
| `JOB_CANCELLED` | 409 | The job was cancelled before it produced a result |
| `MODEL_NOT_FOUND` | 404 | Unknown model alias |
| `ENGINE_NOT_FOUND` | 404 | Unknown engine id |
| `ENGINE_NOT_INSTALLED` | 409 | Engine discovered but its venv is not installed |
| `VOICE_NOT_FOUND` | 404 | Unknown voice id/name (also used for an unknown artifact) |
| `VOICE_REQUIRED` | 422 | No voice exists or was selected and the engine cannot speak without one |
| `INVALID_REF_AUDIO` | 422 | The reference clip could not be decoded or is unusable |
| `TEXT_TOO_LONG` | 413 | A text input exceeded its cap |
| `PATH_OUTSIDE_BASE` | 403 | A caller-supplied path escaped its confinement base |
| `AGENT_MUTATION_DISABLED` | 403 | Mutation is off, or the caller is not on loopback |
| `INVALID_SLUG` | 422 | Engine id / alias failed validation |
| `MANIFEST_INVALID` | 422 | `engine.yaml` could not be parsed or validated |
| `TIMEOUT` | 504 | The operation exceeded its deadline |
| `BACKPRESSURE` | 429 | The per-engine wait queue is full, or a stream consumer stalled |
| `DAEMON_RESTARTED` | 410 | A job was interrupted by a restart and cannot resume |
| `BAD_REQUEST` | 400 | Malformed request, bad field, unknown enum value |
| `INTERNAL_ERROR` | 500 | Unhandled daemon error; read the logs |

A request-body validation failure is a `422` whose `error_code` is also
`BAD_REQUEST`, with the offending field named in `message` — never a bare
`{"detail": …}`.

## HTTP endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness, version, protocol version, `torch_loaded` (open, no auth) |
| `POST` | `/v1/audio/speech` | OpenAI-compatible synthesis, returns audio bytes |
| `POST` | `/v1/agent/speak` | Agent synthesis, returns `SynthesisResult` JSON |
| `POST` | `/v1/audio/speech/pcm-stream` | Chunked 24 kHz mono int16 LE PCM |
| `WS` | `/v1/audio/ws` | Bidirectional PCM with barge-in and `cancel` |
| `POST` | `/v1/voices` | Clone a reference clip (`?overwrite=true` to replace) |
| `GET` | `/v1/voices` | List registered voices |
| `DELETE` | `/v1/voices/{voice_id}` | Delete a voice |
| `POST` | `/v1/voices/design` | Design a voice from a description |
| `POST` | `/v1/clone` | Alias for `POST /v1/voices` with a free-form body |
| `POST` | `/v1/agent/script` | Render a multi-beat script (`?async=true` → job id) |
| `GET` | `/v1/jobs/{job_id}` | Poll a job |
| `POST` | `/v1/jobs/{job_id}/cancel` | Cancel a job |
| `GET` | `/v1/gpu` | GPU/worker/queue state |
| `GET` | `/v1/engines` | Engines, capabilities, install state |
| `POST` | `/v1/engines` | Register an agent-authored engine (mutation gated) |
| `POST` | `/v1/engines/{engine_id}/install` | Run `uv sync` for an engine venv |
| `GET` | `/v1/models` | Models across engines (`?engine_id=` to filter) |
| `POST` | `/v1/models` | Register a model alias (mutation gated) |
| `POST` | `/v1/models/{engine_id}/{model_alias}/download` | Download weights as a job |
| `POST` | `/v1/models/unload` | Unload models (`?force=true` cancels running jobs) |
| `GET` | `/v1/agents/bindings` | List per-agent bindings |
| `PUT` | `/v1/agents/bindings` | Set the calling agent's binding (`X-Agent-Id` required) |
| `DELETE` | `/v1/agents/bindings` | Delete the calling agent's binding |
| `GET` | `/audio/{artifact_id}` | Fetch an artifact (bearer-protected when a key is set) |
| `*` | `/mcp/` | Streamable HTTP MCP (trailing slash required) |

### `GET /health`

```json
{"status": "ok", "version": "0.3.3", "protocol_version": 1, "torch_loaded": false}
```

`torch_loaded` is reported by looking for `torch` in the core daemon's
`sys.modules`. It is `false` on a healthy install; the core venv must never
import torch.

### `POST /v1/audio/speech` (OpenAI-compatible)

Request body (`SpeechRequest`):

| Field | Type | Default | Notes |
|---|---|---|---|
| `input` | string | required | Text to speak |
| `model` | string | `"tts-1"` | `tts-1`, `tts-1-hd`, or `engine:model` |
| `voice` | string | null | Voice name/id, or a builtin OpenAI name (see fallback below) |
| `response_format` | enum | `"wav"` | `wav`, `mp3`, `opus`, `flac`, `pcm` |
| `speed` | float | `1.0` | Playback-rate multiplier passed to the engine |

Returns the audio bytes with `Content-Type` matching the format, a
`Content-Disposition` filename of `<artifact_id>.<format>`, and the headers:

| Header | Value |
|---|---|
| `x-agent-voice-engine` | Engine that produced the audio |
| `x-agent-voice-model` | Model alias |
| `x-agent-voice-speaker` | Present only when a preset-timbre fallback answered |

**Builtin OpenAI voice fallback.** A real OpenAI client sends `alloy`, `echo`, …,
which do not exist locally. Rather than answering `VOICE_NOT_FOUND`, a builtin
name is resolved in this order:

1. a registered voice with that exact name;
2. `AGENT_VOICE_DEFAULT_VOICE_ID`, when set;
3. your only registered voice, when exactly one exists;
4. a preset speaker on the default engine, when a `custom_voice` checkpoint is
   available: `alloy` → `Ryan`, `echo` → `Aiden`, `fable` → `Dylan`,
   `onyx` → `Uncle_Fu`, `nova` → `Serena`, `shimmer` → `Vivian`.

Step 4 is why a fresh install works: a `custom_voice` checkpoint needs no
reference audio. Any other unknown voice name still fails with
`VOICE_NOT_FOUND` — papering over a typo would be worse than an error.

### `POST /v1/agent/speak`

Body is `SpeakRequest`: `text` plus optional `voice`, `speaker`, `instruct`,
`engine`, `model`, `quality` (`high`/`fast`), `language`, `format`, `seed`,
`speed`, `take`, `pronunciations`. Returns a `SynthesisResult` object (see
[Return fields](#return-fields-worth-reading)).

```bash
curl -s -X POST localhost:8000/v1/agent/speak \
  -H 'content-type: application/json' \
  -d '{"text":"Good evening.","speaker":"Serena","format":"wav"}'
```

### `POST /v1/audio/speech/pcm-stream`

The same routing as `speak`, but the body is raw 24 kHz mono int16 LE PCM as it
is generated. Response headers:

| Header | Value |
|---|---|
| `x-agent-voice-engine` | Engine id |
| `x-agent-voice-model` | Model alias |
| `x-agent-voice-streaming-mode` | `native` or `emulated` |
| `x-agent-voice-sample-rate` | `24000` |
| `x-agent-voice-speaker` | Present only when a preset timbre spoke |

If the client stops reading for longer than `STREAM_QUEUE_TIMEOUT_SECONDS`
(default 5 s), the worker stream is cancelled and the request fails with
`429 BACKPRESSURE` (`message`: "client stopped reading the audio stream"). A
stalled consumer never pins VRAM.

### Voices

`POST /v1/voices` — body `CloneVoiceRequest`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `name` | string | required | Must match `^[\w .\-]{1,64}$` |
| `ref_audio_path` | string | required | Absolute path, or relative to `MCP_BASE_PATH` when set; confined as described below |
| `ref_text` | string | `""` | Exact transcript; improves cloning when supplied |
| `x_vector_only` | bool | `false` | Skip the transcript-based path where the backend supports it |

Query: `?overwrite=true` replaces an existing voice with the same name when its
audio differs. Without it, a retry with byte-identical audio returns the existing
voice (idempotent) and a differing clip is rejected.

**Path confinement.** With `MCP_BASE_PATH` set, the path must resolve inside it.
Otherwise, unless `AGENT_VOICE_ALLOW_ANY_PATH=true`, it must resolve inside
`<DATA_DIR>/inbox/`; an escape is `403 PATH_OUTSIDE_BASE` before any ffmpeg work.
With `AGENT_VOICE_ALLOW_ANY_PATH=true` an existing absolute path anywhere on the
machine is accepted. The CLI `agent-voice voices add` bypasses this route and is
always unrestricted.

`GET /v1/voices` — `{"voices": [VoiceMetadata, …]}`, where `VoiceMetadata` is
`voice_id`, `name`, `ref_wav_sha256`, `ref_text`, `x_vector_only_mode`,
`duration_sec`, `created_at`, `warnings`, `source` (`cloned`/`designed`) and
`design_instruct`.

`DELETE /v1/voices/{voice_id}` — `{"deleted": "<voice_id>"}`, or the
`VOICE_NOT_FOUND` error body (HTTP 404).

`POST /v1/voices/design` — body `DesignVoiceRequest`:

| Field | Type | Default | Notes |
|---|---|---|---|
| `name` | string | required | Registered voice name |
| `instruct` | string | required | The description, e.g. "warm baritone, measured pace" |
| `sample_text` | string | built-in anchor | Capped by `MAX_DESIGN_SAMPLE_CHARS` |
| `language` | string | `"English"` | Language of the sample sentence |
| `save_as_clone` | bool | `true` | Register the render as a voice |
| `candidates` | int | `1` | `1..5`; `>1` forces `save_as_clone=false` |
| `take` | int | null | Re-roll the same description |
| `seed` | int | null | Pin the render |
| `overwrite` | bool | `false` | Replace an existing voice with the same name |
| `engine`/`model`/`quality` | — | — | Target selection; the model must be `voice_design` |

Returns `DesignVoiceResult`: `voice_id` (null when not saved), `name`,
`preview_file_path`, `duration_sec`, `source: "designed"`, `design_instruct`,
`engine_id`, `model_alias`, and `candidates` (each with `take`, `seed`,
`file_path`, `download_url`, `duration_sec`).

Design requires the `1.7b-design` checkpoint. `server_info().voice_design_available`
tells you whether it is present.

### Scripts and jobs

`POST /v1/agent/script` — body `ScriptRequest` (`beats`, `default_voice`,
`engine`, `quality`, `format`, `export_srt`, `pronunciations`).

- Synchronous by default: returns `ScriptResult`.
- `?async=true` (legacy `?async_=true` also works) returns
  `{"job_id": "...", "kind": "script"}` immediately and renders in the
  background, reporting per-beat progress.

`GET /v1/jobs/{job_id}` returns a `Job`: `job_id`, `kind`
(`script`/`download_model`/`install_engine`), `status`
(`pending`/`running`/`succeeded`/`failed`/`cancelled`), `progress`, `message`,
`created_at`, `updated_at`, `result`, `error`. A job left pending or running by a
daemon restart is reconciled to failed with `DAEMON_RESTARTED` — it can never
resume, so it never lies as "running".

`POST /v1/jobs/{job_id}/cancel` cancels a job and returns its final record.

### GPU and engines

`GET /v1/gpu` returns `GpuStatus`: `active_engine`, `loaded_models`, `workers`
(engine id → `alive`/`starting`/`dead`), `vram_used_mb`, `vram_free_mb`,
`queue_depth`, `in_flight`, `unload_in_seconds`, `last_used_at`.

`POST /v1/models/unload` (`?force=true|false`) returns
`{"unloaded": [...], "cancelled_jobs": N}`. Without force it answers
`409 WORKER_BUSY` while jobs are running; with force it cancels them and frees
VRAM immediately.

`GET /v1/engines` — `{"engines": [EngineEntry]}`, each with `engine_id`,
`display_name`, `backend_name`, `backend_version`, `installed`, `venv_python`,
capability flags (`streaming`, `pcm_native`, `custom_voice`, `voice_design`,
`instruct_control`), `models` and `why_unavailable`.

`GET /v1/models` — `{"models": [ModelEntry]}` with `engine_id`, `alias`,
`hf_repo`, `quality_tier`, `est_vram_gb`, `is_default`, `downloaded_locally`,
`loaded`, `mode`, `supports_instruct`, `preset_speakers`.

Engine/model mutation (`POST /v1/engines`, `POST /v1/engines/{id}/install`,
`POST /v1/models`, `POST /v1/models/{engine}/{alias}/download`) requires
`ALLOW_AGENT_ENGINE_MUTATION=true` **and** a loopback client, and appends an audit
line to `<DATA_DIR>/mutations.log`.

### Agent bindings

`GET /v1/agents/bindings` lists all bindings. `PUT` and `DELETE` act on the
agent named by the `X-Agent-Id` header (missing header → `400 BAD_REQUEST`).
`SetBindingRequest` is `{voice_id, engine_id, model_alias}`, all optional.

### Artifacts

`GET /audio/{artifact_id}` serves one artifact. The id must be exactly the 16
lowercase hex characters returned by a synthesis call. An unknown but
well-formed id is `404 VOICE_NOT_FOUND` ("Artifacts are ephemeral; re-run
speak()"); path or glob syntax is `400 BAD_REQUEST` and never reaches the
filesystem.

### MCP mount

Streamable HTTP MCP is mounted at `/mcp/` — **with** the trailing slash (`/mcp`
redirects with `307`). Send `X-Agent-Id` to select a binding. The mount is
covered by the same guard, so a key protects `/mcp/*` too.

## WebSocket `/v1/audio/ws`

Bidirectional: the client sends JSON control messages, the server sends binary
PCM frames (24 kHz mono int16 LE) interleaved with JSON events.

Handshake requirements (v0.3.3): a loopback `Host`, no foreign `Origin`, and — if
`AGENT_VOICE_API_KEY` is set — a valid credential via `Authorization: Bearer …`
or `Sec-WebSocket-Protocol: bearer, <key>`. Any failure closes the socket with
code **1008** before `accept()`.

Client → server:

| Message | Fields | Effect |
|---|---|---|
| `{"type": "speak", "text": "..."}` | `text`/`input`, `voice`, `speaker`, `instruct`, `engine`, `model`, `quality`, `language`, `speed`, `pronunciations` | Start synthesis |
| `{"type": "cancel"}` | — | Cancel the in-flight stream |
| `{"type": "ping"}` | — | Liveness check |

Sending a new `speak` while audio is flowing is **barge-in**: the in-flight
generation is cancelled before the next one starts.

Server → client:

| Message | Shape |
|---|---|
| `started` | `{"type":"started","stream_id":"…","engine_id":"…","model_alias":"…","streaming_mode":"native"\|"emulated"}` |
| `PCM` | binary frame, 24 kHz mono int16 LE |
| `done` | `{"type":"done","stream_id":"…"}` |
| `cancelled` | `{"type":"cancelled"}` |
| `pong` | `{"type":"pong"}` |
| `error` | `{"type":"error","error_code":"…","message":"…","suggestion":"…"}` (`suggestion` is present when the daemon has one) |

`text` is capped by `MAX_TEXT_CHARS` on the WebSocket exactly as on HTTP: the
caller is rejected for what it sent, before any rewrite or worker spawn. A
stalled consumer produces `BACKPRESSURE` and closes the generation.

```javascript
// Browser sketch: the key travels in the subprotocol list.
const ws = new WebSocket("ws://127.0.0.1:8000/v1/audio/ws", ["bearer", "secret-key"]);
ws.binaryType = "arraybuffer";
ws.onopen = () => ws.send(JSON.stringify({ type: "speak", text: "Hello." }));
```

## MCP interface

`agent-voice mcp` is a stdio shim that starts the daemon under a file lock if
needed, then proxies to the Streamable HTTP endpoint. That keeps a stdio-only
client from spawning a second GPU owner. Direct HTTP clients can mount
`http://127.0.0.1:8000/mcp/` instead.

20 tools, split into a `core` and a `full` surface by `AGENT_VOICE_TOOLSET`:

| Tool | Surface | Purpose |
|---|---|---|
| `server_info` | core | **Call first.** Free snapshot — version, defaults, voices, engines, `inbox_dir`, GPU state |
| `speak` | core | One utterance → file path. `voice`, `speaker` or `instruct`, per-call `pronunciations` |
| `speak_script` | core | Multi-beat script with exact timings plus SRT/VTT |
| `start_script_job` | core | Start the same render in the background, return a `job_id` |
| `get_job` | core | Poll a job |
| `cancel_job` | core | Cancel a job |
| `clone_voice` | core | Register a voice from a reference clip |
| `design_voice` | core | Invent a voice from a description; optionally save as a clone |
| `list_voices` | core | List registered voices |
| `delete_voice` | core | Delete a voice |
| `gpu_status` | core | Resident models, VRAM, queue depth, in-flight |
| `unload_models` | core | Free VRAM; `force=true` cancels running jobs |
| `set_agent_binding` | full | Bind a voice/engine/model to the calling agent |
| `list_agent_bindings` | full | List bindings |
| `list_engines` | full | Engine discovery and install state |
| `list_models` | full | Model aliases, modes, speakers, download state |
| `download_model` | full | Start a weights download job |
| `register_model` | full | Add a model alias to an engine manifest |
| `register_engine` | full | Stage an agent-authored engine plugin |
| `install_engine` | full | Install/reinstall an engine venv |

`AGENT_VOICE_TOOLSET` values:

| Value | Tools exposed |
|---|---|
| `auto` (default) | `core`, plus the management tools when mutation is enabled |
| `core` | Synthesis, voice clone/design/list/delete, GPU status. No engine/model management |
| `full` | Everything, so the per-call loopback + mutation guard answers with an actionable error |

Resources and prompts:

- `agent-voice://voices` — the voice list as JSON.
- `agent-voice://guide` — this repository's `skills/agent-voice/SKILL.md`.
- `narrate_video` prompt — a starting brief for a narrated video pipeline.

A tool failure is an MCP `isError=true` result whose text is the same
`{error_code, message, suggestion}` JSON, so a client can tell success from
failure without parsing the payload. Audio is never inlined as base64; tools
return paths or URLs.

### Orient with `server_info`

`server_info()` never spawns a worker and never touches the GPU. It answers the
questions that otherwise cost a failed synthesis round-trip:

```json
{
  "version": "0.3.3",
  "default_engine": "qwen3_tts_fast",
  "default_voice": "a1b2c3d4e5f6",
  "voices_count": 1,
  "voices": [{"voice_id": "a1b2c3d4e5f6", "name": "narrator", "duration_sec": 6.4}],
  "installed_engines": ["qwen3_tts_fast"],
  "inbox_dir": "/repo/data/inbox",
  "preset_speakers": ["Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ryan",
                      "Aiden", "Ono_Anna", "Sohee"],
  "voice_design_available": true,
  "engines": [{"engine_id": "qwen3_tts_fast", "installed": true, "streaming": true,
               "why_unavailable": null}],
  "gpu_status": {"active_engine": null, "loaded_models": [], "vram_used_mb": 631},
  "next_steps": []
}
```

| Field | Read it as |
|---|---|
| `voices_count` | `0` means you must clone a voice before a clone-only checkpoint can speak |
| `inbox_dir` | Where an agent-supplied `ref_audio_path` must live unless `MCP_BASE_PATH` is set |
| `preset_speakers` | Names `speak(speaker=…)` accepts on the default engine *right now*; empty means only a clone or a design can produce audio |
| `voice_design_available` | Whether `design_voice()` can run without another download |
| `installed_engines` | Engines that can actually run; `engines[].why_unavailable` explains the rest |
| `next_steps` | Advisory list of suggested next actions, added in v0.3.3: empty when the default engine is installed, weights are cached and at least one voice or preset speaker is available; otherwise short copy-pasteable actions such as "Run `agent-voice engines install qwen3_tts_fast`." or "Put a 3-10 s reference clip in `<inbox_dir>` and call `clone_voice()`, or call `design_voice()`." It is advice, never an error — tool calls still enforce their own rules |

Speaker lists for another engine come from `list_models(engine_id)`; pass
`engine=` to `speak`/`speak_script` to use it.

## Return fields worth reading

`SynthesisResult` (from `speak`, `POST /v1/agent/speak`) carries more than a
path:

| Field | Meaning |
|---|---|
| `file_path` | Absolute local path — read it directly |
| `download_url` | Relative HTTP URL for the same artifact, for handing to another process |
| `chunks_used` | How many text chunks were stitched. `>1` means the input exceeded `CHUNK_CHARS` and was split — normal, not an error |
| `elapsed_ms` | Wall-clock cost; near zero on a cache hit, so latency regressions are obvious |
| `cached` | `true` when the deterministic disk cache answered and no GPU work happened |
| `mode` | Which mode produced the audio: `clone`, `custom_voice` or `voice_design` |
| `speaker` | The preset timbre that spoke, when a `custom_voice` checkpoint answered |
| `take` | Which alternate delivery was rendered (`take=2`, …); `null` for the canonical take |
| `realtime_multiple` | `duration_sec / elapsed_sec`. Above `1.0` is faster than real time, below is slower, `null` when the duration is unknown or the audio was streamed. On a cache hit `elapsed_ms` is only the lookup, so the ratio is meaningless there — read it from a real render and pair it with `cached` |
| `real_time_factor` | **Deprecated.** The v0.2 name for `realtime_multiple`; still populated for one release and removed afterwards. New code should read `realtime_multiple` |

Also present: `artifact_id`, `duration_sec`, `sample_rate` (always 24000),
`format`, `engine_id`, `model_alias`, `backend_name`, `backend_version`,
`voice_id`, `streaming_mode` (`native`/`emulated`).

Reference point: a warm 0.6B render measured **≈ 0.86** (slightly slower than
real time) on the reference card.

## Alternate takes

`take=2` (or `3`, …) renders a different read of the same line, on `speak`, on a
`design_voice` request, or on an individual `speak_script` beat. Each take is a
separate cache entry, so asking again for the same take is free and take 1 still
resolves to the original audio. When you pass `take` without `seed`, the seed is
derived from the take, so a given take is reproducible.

`design_voice(..., candidates=2..5)` takes this further: it renders several takes
in one call, saves none, and returns a `candidates` list (`take`, `seed`,
`file_path`, `download_url`, `duration_sec`). Take 1 is also
`preview_file_path`, so a caller that ignores the list still has audio to play.
Re-run with `take=<n>` and `save_as_clone=true` (no `seed`) and the winning take
is registered from the cache instead of being synthesised again.

## Voice names

A voice name must match `^[\w .\-]{1,64}$` — letters, digits, underscores,
spaces, dots and hyphens, 1–64 characters. Path separators, newlines and control
characters are rejected so a name cannot smuggle anything into metadata, logs or
JSON. The rule is enforced for `clone_voice`, `design_voice` and `voices add`,
and applies to a designed voice exactly as to a cloned one.

Pick a stable label: `speak(voice="narrator")` and script beats address the voice
by that exact name (or its id) from then on.

## Input caps

Four caps bound what a single request can ask the daemon to do. They are
enforced at admission — before routing, before a worker is spawned and before any
GPU time is spent.

| Setting | Default | Applies to | Violation |
|---|---|---|---|
| `MAX_SCRIPT_BEATS` | 200 | beats in one `speak_script` / `POST /v1/agent/script` | `400 BAD_REQUEST` |
| `MAX_INSTRUCT_CHARS` | 1000 | `instruct` on `speak`, a beat, or `design_voice` | `413 TEXT_TOO_LONG` |
| `MAX_DESIGN_SAMPLE_CHARS` | 500 | `design_voice(sample_text=…)` | `413 TEXT_TOO_LONG` |
| `MAX_REF_TEXT_CHARS` | 2000 | `clone_voice(ref_text=…)` | `413 TEXT_TOO_LONG` |

They sit alongside `MAX_TEXT_CHARS` (one utterance, default 20000) and
`CHUNK_CHARS` (default 400 — how a long text is split, not rejected). A rejected
request creates no cache entry and leaves no scratch directory, so retrying it
unchanged fails identically. Shorten the input, split a long script, or raise the
cap deliberately.

## Scripts: beats, timings and multi-speaker

`speak_script` takes a list of beats; every key is optional except `text`:

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
| `voice` | Voice id or name for **this beat** — enables multi-speaker scripts; the result's `beats[].voice_id` records who spoke |
| `speaker` | Preset timbre for this beat (`Vivian` … `Sohee`), answered by a `custom_voice` checkpoint. No reference clip needed |
| `instruct` | Delivery direction. With `speaker` it steers that preset and needs `supports_instruct`; with no `voice`/`speaker` it routes the beat to `voice_design`; a cloned beat ignores it |
| `engine` / `model` / `quality` | Per-beat overrides. Omit `quality` to inherit the script's quality |
| `language` | `"Auto"` or a language name |
| `target_duration_sec` + `fit_mode` | Fit the beat into a fixed slot (`none`, `pad`, `atempo`) |
| `pause_after_sec` | 0–10 s of silence **after** the beat, included in the reported timings |
| `take` | Alternate delivery of this beat's line |

`quality` is the one key whose default is worth knowing: a beat leaves it unset
to inherit `speak_script`'s `quality`, and sets it only to make one beat cheaper
or better. (Before v0.3.1 every beat defaulted to `high`, which quietly overrode
a `quality="fast"` script.)

`BeatTiming` reports `start_sec`, `end_sec`, `pause_after_sec`, `voice_id`,
`mode`, `speaker`, `instruct`, `cached` and `artifact_id` per beat. `end_sec` is
the end of the spoken portion; the next beat starts at
`end_sec + pause_after_sec`. That is what makes a Manim or slideshow timeline
land on the frame you expect.

### The `.timing.json` file

With `export_srt=true` (the default) a script render writes
`<artifact>.timing.json` next to the audio, alongside `.srt` and `.vtt`. It is
the serialised `ScriptResult`:

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
      "mode": "clone",
      "speaker": null,
      "instruct": "",
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
      "mode": "clone",
      "speaker": null,
      "instruct": "",
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
| `beats[].duration_sec` | Spoken length of this beat, **excluding** any trailing pause |
| `beats[].start_sec` / `end_sec` | Onset and end of the spoken portion; every earlier pause is already baked in |
| `beats[].voice_id` | Which voice actually spoke this beat — auditable multi-speaker scripts |
| `beats[].mode` / `speaker` / `instruct` | How the beat was rendered |
| `beats[].cached` | `true` when the beat was reused from disk. Edit one beat of twelve and see eleven instant hits |
| `beats[].artifact_id` | This beat's own artifact, so one beat can be fetched or re-rendered |
| `srt_path` / `vtt_path` / `timing_json_path` | Where the sidecars landed |

### Consuming it from Manim

Read `start_sec` / `end_sec` instead of accumulating offsets yourself — every
earlier `pause_after_sec` is already baked into `start_sec`:

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
        cursor = 0.0  # scene time already rendered
        for position, beat in enumerate(beats):
            gap = beat["start_sec"] - cursor
            if gap > 0:
                self.wait(gap)
                cursor += gap
            caption = Text(beat["text"]).scale(0.7)
            self.play(FadeIn(caption), run_time=FADE_IN)
            cursor += FADE_IN
            remaining = (beat["end_sec"] - beat["start_sec"]) - FADE_IN
            if remaining > 0:
                self.wait(remaining)
                cursor += remaining
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

A runnable end-to-end version lives in
[`examples/manim_demo/`](../examples/manim_demo/): it asks for the script, reads
`.timing.json`, times the scene from it, renders with Manim, and muxes the
narration with ffmpeg. It prints the per-beat timings and the drift between the
audio and video durations, and exits non-zero if they disagree by more than
750 ms.

```bash
uv pip install -e ".[demo]"      # adds manim; the core daemon stays lean
uv run python examples/manim_demo/run_demo.py --engine mock   # no GPU needed
```

## Pronunciation lexicon

Some words are written one way and spoken another: `kubectl`, a surname, an
acronym. The lexicon rewrites them before synthesis without changing the text you
asked to speak.

- **Persistent map.** `data/pronunciations.json` is a plain `{word: replacement}`
  object. Matching is case-insensitive and whole-word, so `async` never rewrites
  `asynchronously`, and a multi-word key is matched before any shorter key it
  contains. The file is read on every request, so an edit reaches a running
  daemon immediately.
- **Per-call overrides.** `speak(..., pronunciations={"kubectl": "cube control"})`
  and `speak_script(..., pronunciations={...})` merge on top of the persistent
  map for that call only; nothing is written to disk. On a script the map applies
  to every beat.
- **The cache follows the spoken text.** Rewriting happens before the cache key is
  computed, so editing a term invalidates only lines that actually use it; adding
  an unrelated entry leaves existing renders cached. The lexicon is capped at
  **500 entries**, with keys and replacements at most **200 characters** each
  (enforced by the CLI/store and per-call maps).
- **Subtitles keep the written text.** `.srt`, `.vtt` and `.timing.json` carry the
  beat text you passed in, not the rewritten spoken form — captions show
  `kubectl`, the audio says "cube control".

Manage the persistent map from a shell, no daemon required:

```bash
agent-voice pronunciations list
agent-voice pronunciations set kubectl "cube control"
agent-voice pronunciations delete kubectl
```

`set` replaces an existing entry case-insensitively and prints the resulting map
as JSON; `delete` removes every casing of the word, prints `{"deleted": "<word>"}`
and exits non-zero when the word is not in the lexicon.

## OpenAI-compatible clients

`POST /v1/audio/speech` is drop-in for an OpenAI SDK client: `input`, `voice`,
`response_format` and `speed` all work, and `model` accepts `tts-1` (fast tier),
`tts-1-hd` (high tier) or an explicit `engine:model` selector. The voice fallback
rules are described [above](#post-v1audiospeech-openai-compatible).

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="not-needed-unless-set")
with client.audio.speech.with_streaming_response.create(
    model="tts-1", voice="nova", input="Local speech, delivered over the OpenAI shape."
) as response:
    response.stream_to_file("out.wav")
```

If `AGENT_VOICE_API_KEY` is set, pass it as `api_key` (the daemon expects
`Authorization: Bearer <key>`); otherwise any placeholder works.

LangChain and other MCP-speaking frameworks are configured as MCP clients —
see [install/agent.md](install/agent.md).
