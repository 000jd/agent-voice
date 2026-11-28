# Configuration

All configuration is environment variables, read through `pydantic-settings`
(`src/agent_voice/config.py`). `.env.example` is the commented template; copy it
once, edit it, or let `agent-voice setup` write it:

```bash
cp .env.example .env
```

## How settings are loaded

- **One file: `<home>/.env`.** The home is `AGENT_VOICE_HOME` when set, otherwise the
  first parent of the installed package that holds both `pyproject.toml` and `engines/`
  (the checkout, or the installer's install directory), otherwise `~/.agent-voice`.
  `get_settings()` constructs `Settings(_env_file=home / ".env")`, so neither the current
  working directory nor a systemd unit's `WorkingDirectory=` decides which config is
  read.
- Precedence: **process environment** > `<home>/.env` > code defaults. Names are
  case-insensitive; unknown variables are ignored. A bare `Settings(...)` reads **no**
  `.env` at all (`env_file=None`), which keeps tests hermetic; pass
  `Settings(_env_file=…)` to opt in.
- Settings are read once (cached) at startup. Editing `.env` requires a daemon
  restart. The one exception is the pronunciation lexicon — `data/pronunciations.json`,
  `data/pronunciations/<project>.json` and each voice's `pronunciations.json` — which
  is re-read on every request.
- Relative `DATA_DIR`/`ENGINES_DIR` values resolve **against the home**, never against
  the cwd, so `agent-voice` works from any directory. Absolute values are unchanged.
- A handful of values are validated at load: `NORMALIZE_LUFS` must be between
  `-40` and `-5`, `MAX_ACTIVE_ENGINES` between `1` and `2`, and all caps must be
  `>= 1`. An unknown device name in `AGENT_VOICE_DEVICES` fails at load; an unknown
  engine id or a device the manifest does not declare fails at startup and in `doctor`.
- `.env.example` ships `AGENT_VOICE_DEFAULT_ENGINE=qwen3_tts_fast`; the **code**
  default is `mock`, so a checkout with no `.env` at all starts on the CPU mock
  engine.

## Server

| Variable | Default | Effect |
|---|---|---|
| `AGENT_VOICE_HOST` | `127.0.0.1` | Bind address. A non-loopback value without an API key refuses to start. IPv6 literals (`::1`) work and are bracketed in generated URLs |
| `AGENT_VOICE_PORT` | `8000` | Bind port, and the base for every generated `download_url` |
| `AGENT_VOICE_API_KEY` | *(empty)* | When set, `/v1/*`, `/audio/*`, `/mcp/*`, `/docs`, `/redoc` and `/openapi*` require `Authorization: Bearer <key>`; `/health` stays open. `/audio` artifacts need the header too, so a `download_url` is not a capability. WebSockets accept the same header or `Sec-WebSocket-Protocol: bearer, <key>`. A whitespace-only value counts as unset |
| `AGENT_VOICE_UI` | `on` | Serve the packaged Studio SPA at `/ui`. `agent-voice serve --no-ui` overrides it. `off` registers no UI route and imports no UI module. The API remains bearer-guarded; UI assets are public on the allowed loopback origin |

## Defaults

| Variable | Default | Effect |
|---|---|---|
| `AGENT_VOICE_DEFAULT_ENGINE` | `mock` in code, `qwen3_tts_fast` in `.env.example` | Engine used when a request names none |
| `AGENT_VOICE_DEFAULT_ASR_ENGINE` | `phonon` | Which **`kind: asr`** engine a transcription uses when none is named. Deliberately independent of `AGENT_VOICE_DEFAULT_ENGINE`: speech in and speech out are different plugin families, and a CPU listener next to a GPU speaker is the normal deployment. If it is not installed, `doctor` warns (never fails) and `transcribe` answers `ENGINE_NOT_INSTALLED` |
| `AGENT_VOICE_DEFAULT_VOICE_ID` | *(empty)* | Voice used when a request names none. When empty and exactly one voice is registered, that voice is the default |

## Hardware

| Variable | Default | Effect |
|---|---|---|
| `AGENT_VOICE_DEVICE` | `auto` | Global device preference, applied to every engine that supports it: `auto` · `cpu` · `cuda` · `mps` · `rocm`. `auto` resolves per engine from its manifest and the host. A value an engine does not support is a `BAD_REQUEST` naming the supported list — never a silent fallback |
| `AGENT_VOICE_DEVICES` | *(empty)* | Per-engine overrides in the single comma-separated env spelling `kokoro=cpu,qwen3_tts=cuda`. An unknown *device* name fails at load; an unknown *engine* id, or a device that engine's manifest does not declare, fails at startup and in `doctor`, naming the engine, the requested device and the supported list |

Resolution order for one engine:

1. `AGENT_VOICE_DEVICES[<engine_id>]`, when set;
2. a non-`auto` `AGENT_VOICE_DEVICE`;
3. the manifest's `default_device`;
4. the host's accelerator — `cuda` on NVIDIA, `mps` on Apple silicon, `rocm` on AMD,
   otherwise `cpu` (only reached by a manifest that leaves `default_device` unset).

The result is then validated twice: against the manifest's `supported_devices` (an
engine that cannot do it is a `BAD_REQUEST` naming the supported list) and against what
the host can serve (a `cuda` resolution on a CPU-only box is a `BAD_REQUEST` naming the
detected accelerator and suggesting `AGENT_VOICE_DEVICE=cpu`). That second check is a
refusal, not a silent reassignment: `auto` is not a "pick something that works" mode for
an engine whose declared default is an accelerator this machine lacks. On a CPU-only or
Apple-silicon box, pin it — `AGENT_VOICE_DEVICE=cpu`, or
`AGENT_VOICE_DEVICES=qwen3_tts=cpu` for one engine. `agent-voice setup` writes exactly
that pin when its recommendation differs from a manifest default. `auto` also never
selects an `experimental` pair where a verified default exists; `unsupported` is refused
whatever `--experimental` says. The honest matrix is
`DEVICE_SUPPORT` in [`src/agent_voice/hardware.py`](../src/agent_voice/hardware.py); it is
reproduced in the [README](../README.md#device-support) and
[engines.md](engines.md#device-variants), and every row is backed by
[phase0-devices-report.md](phase0-devices-report.md).

**A device is a dependency set, not a runtime flag.** Each committed `uv.lock` bakes the
torch wheel index into every torch entry, and CPU and CUDA already lock different torch
versions — so switching an engine's device **reinstalls that engine's venv**:

```bash
agent-voice engines install qwen3_tts --device cpu              # verified
agent-voice engines install kokoro --device cuda --experimental # experimental
```

Only one variant per engine is resident at a time. A venv whose torch build cannot serve
the resolved device is a clear `409 ENGINE_NOT_INSTALLED` that names the reinstall
command, not a crash. `--experimental` only grants permission: a pair also needs a shipped
variant project and lock under `engines/<id>/variants/<device>/`, so a pair the report
calls `experimental` can still be refused until that variant is published —
`engines install <id> --device <d> --dry-run` prints the available ones.

`qwen3_tts_fast` supports `cuda` only (its package requires an NVIDIA GPU), and `phonon`
has no CUDA wheel at all — its NVIDIA path is a container and is not supported here. The
**0 MB VRAM** claim belongs to `device=cpu` alone: `kokoro` and `phonon` on cuda/mps
report real memory and are `experimental`.

`agent-voice doctor` prints a `hardware` row (OS/arch, cores, RAM, accelerator) and a
`device` row per installed engine with its resolved device and `verified`/`experimental`
level. `agent-voice setup --dry-run` prints the same detection plus the recommendations
without touching anything.

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
| `MAX_TRANSCRIBE_SECONDS` | `3600` | Refuse media longer than this at admission, before ffmpeg extraction or a worker: a podcast-length cap on **work**, not on file size. Applies to `transcribe` and `dub_prepare`. Violation: `400 BAD_REQUEST` naming the cap |
| `MAX_DUB_SEGMENTS` | `500` | Cap on translated lines in one `dub_render` / `dub/jobs`. Checked before any synthesis, so a runaway render is refused in milliseconds |
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

## Studio ingestion and retention

| Variable | Default | Effect |
|---|---|---|
| `INGEST_MAX_BYTES` | `536870912` | Maximum accepted upload body before parsing and while staging. Oversized is `413 MEDIA_TOO_LARGE` |
| `INGEST_MAX_CHANNELS` | `8` | Reject a media stream above this channel count |
| `INGEST_MAX_SAMPLE_RATE` | `192000` | Reject a stream above this rate |
| `INGEST_MAX_STREAMS` | `8` | `-max_streams` and probe admission |
| `INGEST_MAX_DECODED_BYTES` | `268435456` | Decoded WAV output cap; `ffmpeg -fs` plus a post-stat |
| `INGEST_PROBE_TIMEOUT_SECONDS` | `5` | Hard ffprobe deadline |
| `INGEST_CONCURRENCY` | `2` | Reserved for the global ingest semaphore |
| `VOICE_DRAFT_TTL_SECONDS` | `3600` | Draft lifetime; expired drafts are pruned on startup and access |
| `VOICE_DRAFT_MAX_COUNT` | `8` | Global live-draft count quota |
| `VOICE_DRAFT_MAX_BYTES` | `536870912` | One draft's stored byte cap |
| `VOICE_DRAFT_QUOTA_BYTES` | `2147483648` | Global draft storage quota |
| `VOICE_DRAFT_MAX_DURATION_SECONDS` | `60` | Draft acceptance duration; commit still clamps 1–30 s |
| `HISTORY_MAX_ITEMS` | `2000` | Persistent generation history retention |
| `CAPTURE_MAX_ITEMS` | `500` | Persistent transcription capture retention |
| `CAPTURE_MAX_TOTAL_BYTES` | `5368709120` | Capture audio/metadata budget |
| `CAPTURE_MAX_TRANSCRIPT_CHARS` | `200000` | Inline transcript edit cap |
| `ARTIFACT_TTL_SECONDS` | `604800` | Reserved for unreferenced-artifact pruning |

Untrusted media is sniffed and forced through a closed demuxer table
(`wav`, `flac`, `ogg`, `matroska`, `mov`, `mp3`, `aac`, `mpegts`), with
`-protocol_whitelist file`, `-format_whitelist`, bounded probe/decode time,
`-max_streams`, input `-t`, output `-fs`, `stdin=DEVNULL`, sanitized environment
and process-group kill. Playlists, concat scripts, archives and unknown
signatures are `415`. The daemon refuses to fall back to auto-demuxing.

`agent-voice serve --no-ui` is the command-line override for
`AGENT_VOICE_UI=off`; it takes effect before the app factory reads settings.

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
| `hotwords` | 25 terms | each term at most 64 characters |

`MAX_HOTWORDS` lives in `schemas.py` and is exposed as `settings.max_hotwords` for
callers, but it is not an environment variable: an unbounded bias list is unbounded
decoder work, the same reasoning that keeps the lexicon caps structural.

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

Media confinement is a path setting, not a tool setting: `MCP_BASE_PATH` — alias of
`AGENT_VOICE_MCP_BASE_PATH` — is documented in [Paths](#paths).

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

| Variable | Prefixed alias | Default | Effect |
|---|---|---|---|
| `AGENT_VOICE_HOME` | — | checkout root, else `~/.agent-voice` | The one anchor. `get_settings()` reads `<home>/.env`; relative paths below resolve against it |
| `AGENT_VOICE_DATA_DIR` | `DATA_DIR` | `data` → `<home>/data` | Root for `voices/`, `inbox/`, `cache/`, `jobs/`, `dubs/`, `artifacts/`, `pronunciations/`, `bindings.json`, `pronunciations.json`, `daemon.lock`, `daemon.pid`, `daemon.log`, `mutations.log`. Resolved to an absolute path at load |
| `AGENT_VOICE_ENGINES_DIR` | `ENGINES_DIR` | `engines` → `<home>/engines` | Where engine plugins are discovered and their venvs live. Resolved to an absolute path at load |
| `AGENT_VOICE_MCP_BASE_PATH` | `MCP_BASE_PATH` | *(empty)* | Confines `ref_audio_path` reads to this directory **and** relocates artifacts to `<MCP_BASE_PATH>/artifacts`, so a returned `file_path` stays readable inside an agent sandbox. Takes precedence over `AGENT_VOICE_ALLOW_ANY_PATH` |

Relative values are resolved against the **home**, never against the daemon's working
directory, because workers are spawned with `cwd=<engine dir>` and because the CLI must
behave identically from any directory. Absolute values are unchanged. `agent-voice doctor`
prints the resolved `home`, `data`, `engines` and `config` paths.

**Aliases.** `DATA_DIR`, `ENGINES_DIR` and `MCP_BASE_PATH` are accepted as unprefixed
aliases of the `AGENT_VOICE_*` spellings; when both are set the **prefixed** one wins. New
files should use the prefixed spelling — `DATA_DIR` is generic enough to collide with
another tool's `.env`.

### `DATA_DIR` layout

```text
data/
├── voices/<voice_id>/         reference clip, metadata, per-voice lexicon
├── inbox/                     the confinement root for agent-supplied paths;
│                              OpenAI transcription uploads land here and are deleted
├── cache/                     deterministic synthesis cache (MAX_CACHE_GB)
├── jobs/                      durable job records
├── dubs/<dub_id>/             dubbing work dir: asr.wav (16 kHz), bed.wav (24 kHz stereo),
│                              speaker_ref.wav, project.json. Removed after a successful
│                              render; kept on failure so a retry can reuse it
├── artifacts/                 rendered audio, video, transcripts, subtitles, .timing.json
│                              (relocated to <MCP_BASE_PATH>/artifacts when that is set)
├── pronunciations.json        global lexicon
├── pronunciations/<project>.json
├── bindings.json
├── mutations.log
└── daemon.{lock,pid,log}
```

`data/dubs/` is the only directory that grows and is not garbage-collected: a
prepared-but-never-rendered dub is kept on purpose, so a translation session can span
a restart. Delete `<DATA_DIR>/dubs/` to reclaim the space.

## Engine prerequisites

Some engines need a system package or a model that is not a pip dependency. These
are read-only checks in `agent-voice doctor`; none of them is an environment
variable.

| Prerequisite | Needed by | If missing |
|---|---|---|
| `ffmpeg` + `ffprobe` | every engine (reference canonicalisation, format conversion, 16 kHz ASR extraction, dubbing mux) | `FAIL` row — the daemon cannot produce non-WAV audio or transcribe anything |
| `sidechaincompress`, `alimiter` filters | dubbing with `keep_original_bed=true` | `dub filters` `WARN` row. The dub still renders, with a plain `amix` instead of a ducked mix |
| `espeak-ng` | `kokoro` (English out-of-dictionary fallback, non-English G2P) | `WARN` row with the platform install command; common English still speaks |
| `en_core_web_sm` (spaCy) | `kokoro` English G2P | pinned as a direct dependency in `engines/kokoro/pyproject.toml`, so `engines install kokoro` brings it |
| CPU-only `torch` | `kokoro`, `phonon` (on their `cpu` variant) | pinned to the `pytorch-cpu` index in each engine's `pyproject.toml` and `variants/cpu/`; a CUDA wheel there would defeat the zero-VRAM guarantee |
| A device variant that matches the resolved device | any engine you set `AGENT_VOICE_DEVICE`/`AGENT_VOICE_DEVICES` for | `409 ENGINE_NOT_INSTALLED` naming `agent-voice engines install <id> --device <d>`; see [Hardware](#hardware) |
| NVIDIA driver + GPU | `qwen3_tts`, `qwen3_tts_fast` | `WARN` row ("mock/CPU only"); `kokoro`, `phonon` and the mock engines still work |
| A `kind: asr` engine of any sort | `transcribe`, `dub_prepare`, `dub_render` | `asr` `WARN` row naming `agent-voice engines install <AGENT_VOICE_DEFAULT_ASR_ENGINE>`; `server_info().asr_engines` is empty |

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
| `AGENT_VOICE_RESOLVED_DEVICE` | The device the manager resolved for this engine (`cpu`, `cuda`, `mps`, `rocm`). Always overwritten from the resolution result — never `setdefault` — so a stale inherited value cannot silently change the backend |

## Changing a setting safely

1. Edit `<home>/.env` (the path `agent-voice doctor` prints as `config=`), not the host
   environment, so the value is reviewable in the repo. `agent-voice setup` edits the same
   file, preserving comments and unknown keys and leaving a `.env.bak` behind.
2. Restart the daemon (`Ctrl-C` then `agent-voice serve`, or
   `systemctl --user restart agent-voice`).
3. Confirm with `agent-voice doctor` (environment, `ffmpeg`, the GPU, `espeak-ng`
   when Kokoro ships, engines, models, the port, the detected hardware, the resolved
   home and each engine's device) and `agent-voice status` (health +
   GPU state). `server_info()` reports the effective `default_engine`,
   `default_voice`, `inbox_dir` and GPU snapshot from inside an agent.
4. For anything security-relevant (`ALLOW_AGENT_ENGINE_MUTATION`,
   `AGENT_VOICE_ALLOW_ANY_PATH`, `AGENT_VOICE_API_KEY`,
   `AGENT_VOICE_ALLOWED_HOSTS`), read [SECURITY.md](../SECURITY.md) first.
