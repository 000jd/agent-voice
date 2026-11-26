# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Dates are the commit dates (`git log --format='%h %cs %s'`); short hashes are
given for every release so a tag can be verified against the history.

## [Unreleased]

No unreleased changes.

## [0.4.0] — 2026-11-26 — unreleased

"Expressive markup, a zero-GPU Kokoro engine, scoped lexicons and code mode."

### Added

- **Inline expressive markup**, on by default (`markup: true`) on `speak`,
  `speak_script` beats, the WebSocket `speak` frame and the PCM stream:
  `[pause]` / `[pause 500ms]` / `[pause 1.5s]` (0–10 s), `[slow]` / `[fast]`
  (×0.82 / ×1.22, clamped 0.5–2.0), `[emphasis]` (+2.5 dB and 0.94×, applied at
  stitch time, peak-limited), `[spell]` (character by character) and blank-line
  paragraph gaps (`paragraph_gap_ms`, 0–5000). Non-whitelisted brackets (`arr[0]`,
  `matrix[i][j]`, `[1, 2]`) stay literal; a recognised tag with a malformed value
  is `400 BAD_REQUEST`; caps (128 tags, depth 4, 32 segments) are enforced before
  a worker spawns. `markup=false` passes every bracket through untouched.
  Subtitles keep the written text (`display_text`).
- **Kokoro-82M CPU engine** (`engines/kokoro/`, `kokoro==0.9.4`, Apache-2.0):
  native 24 kHz mono, native PCM streaming and **0 MB of VRAM** — CPU-only torch
  from the `pytorch-cpu` index, `CUDA_VISIBLE_DEVICES=""` and `device="cpu"` before
  torch is imported. Eight English preset voices (`af_heart` … `bm_lewis`).
  Measured 3.4–5.2× real time and 1.37 GB peak RSS on a Ryzen 7 5700X. No cloning,
  no voice design, no `instruct`. The spike is recorded in
  `docs/phase0-kokoro-report.md`.
- **Scoped lexicons**: `data/pronunciations/<project>.json` (validated `project`
  slug) and `<voices>/<voice_id>/pronunciations.json`, merged
  global → project → voice → per-call. 500 entries per scope, 1,500 merged,
  validated on load and again before compile.
- **Code normalization** (`spell_identifiers`, default `false`): `snake_case`,
  `camelCase`/`PascalCase`, versions, paths, operators and `C++`, applied after the
  lexicon and only to its complement. `[spell]` spans are protected from both.
- CLI: `pronunciations list|set|delete --project|--voice`, `pronunciations show`
  (every layer, the merged map and `pronunciation_hash`) and
  `pronunciations check <text-or-file> [--spell-identifiers]` — a read-only
  prediction of what will be rewritten, plus unknown ALL-CAPS acronyms and
  code-like identifiers with both their `[spell]` and code-mode readings. No
  synthesis, no daemon.
- `markup`, `spell_identifiers`, `paragraph_gap_ms` and `project` on
  `SpeakRequest`, `ScriptRequest` and (as inheritable overrides) `BeatSpec`;
  `BeatTiming.had_markup`; the same selectors on the MCP tools, the WebSocket
  frame and the PCM stream.
- `doctor`: an `espeak-ng` row whenever the Kokoro engine is discovered — PASS when
  present, WARN with the platform install command when missing, never FAIL for
  common English.
- OpenAI builtin-voice fallback for Kokoro: `alloy` → `af_heart`,
  `echo` → `am_adam`, `nova` → `af_bella`, `onyx` → `am_michael`,
  `fable` → `bm_george`, `shimmer` → `bf_emma`.

### Changed

- `engines test` is **mode-aware**: a clone model is still preferred, but an engine
  with no clone checkpoint (Kokoro) is exercised through its first `custom_voice`
  model's first preset speaker, with `mode` and `speaker` sent on the synthesize and
  stream steps. Clone engines keep the original v1 path exactly.
- **Cache key:** `CACHE_KEY_FIELDS` is **unchanged**. The rewritten spoken text and
  the effective `speed` already enter the key; pauses and paragraph gaps are local
  silence and are never cached; `[emphasis]` gain is applied at stitch time, after
  the segment cache; `project` is deliberately not keyed; `pronunciation_hash` is
  observability only. Upgrading invalidates no existing render.
- Docs: markup grammar and precedence, scoped-lexicon precedence, the Kokoro install
  and measurement, the new caps, and the v0.3.3 corrections below.
- `pyproject.toml`: version `0.4.0`, `kokoro` / CPU keywords and classifiers. No
  dependency changes; the core venv still installs no torch.

### Security

- **H1** `agent-voice serve --host 0.0.0.0` now validates the **effective**
  `(host, port)` before `uvicorn.run`, so an unauthenticated LAN bind is refused
  even when `.env` says loopback (`--port 0` is still honoured).
- **M7** pronunciation caps are enforced on load and again before compile, so a
  hand-edited `data/pronunciations.json` or a raw WS/OpenAI map cannot compile an
  unbounded regex.
- **L8** a whitespace-only `AGENT_VOICE_API_KEY` is unset rather than a valid key,
  and a non-loopback bind with it still refuses to start.
- **L2** one path resolver in `security.py` now serves both MCP and REST
  `clone_voice`; **L3** the WebSocket subprotocol echo preserves the client's
  spelling; **L4** trailing-dot FQDNs (`localhost.`) are accepted; **L5**
  `is_loopback_bind` uses `ipaddress`, so `127.evil.example` is not loopback.
- **M2** the documented WebSocket refusal is corrected: the pre-`accept()` rejection
  is an **HTTP 403** over real uvicorn, not a `1008` close (only Starlette's
  in-process TestClient fabricates `1008`).

### Fixed

- **H2** a forced unload while a job is waiting no longer wedges the engine lock for
  the rest of the daemon's life.
- **M1** a worker death mid-stream is reported as a structured WS error / aborted
  HTTP stream instead of a clean, apparently complete stream.
- **M3** `doctor` reports an invalid `.env` as a `config` FAIL row instead of
  tracing back; **M4** the effective default engine is validated even when `.env` is
  absent; **M5** the port probe is address-family aware (`AGENT_VOICE_HOST=::1`
  works) and `base_url` brackets IPv6 literals.
- **M6** `server_info().next_steps` no longer recommends `design_voice()` when no
  design checkpoint exists; **M8** a malformed WebSocket JSON frame, or a
  recoverable request error, no longer closes the socket.
- **L6** `stream()` releases the engine lock if it raises between `_begin_job` and
  the `try`; **L7** `serve --port 0` is honoured.
- **L10** the v0.3.3 entry below now pins the audited tag commit `3cce966` (it said
  `2650a4a`).
- Docs-only corrections: `/redoc` is listed as unauthenticated, `MAX_QUEUE_DEPTH` is
  documented as global rather than per-engine, `streaming_mode` is documented as
  present only on streamed responses, the `NORMALIZE_LUFS` wording now matches the
  code (WAV is a bit-exact byte copy, `pcm` is never normalised, scripts are not
  normalised), `server_info`'s field list is complete, and `/audio` URLs are
  documented as needing the bearer header.

### Known limitations

- `models download --no-wait` is accepted but ignored (L1): the CLI always blocks
  until the snapshot completes.
- WAV output is intentionally bit-exact rather than loudness-normalised (L9).

## [0.3.3] — 2026-11-26 — `3cce966`

Security-hardening and governance release. No dependency changes; the core venv
still installs no torch.

### Security

- **WebSocket handshake now follows the same rules as HTTP (S1).** Host/Origin
  and Bearer enforcement moved into a pure ASGI `GuardMiddleware` that runs for
  both `http` and `websocket` scopes. A foreign `Host` is `421`, a foreign
  `Origin` is `403`, and a WebSocket that fails either — or lacks a valid
  credential — is closed with code **`1008`** *before* `accept()`. Previously a
  WebSocket bypassed the Starlette HTTP middleware entirely.
- WebSocket credentials may arrive as `Authorization: Bearer <key>` or as
  `Sec-WebSocket-Protocol: bearer, <key>` (browsers cannot set headers on a
  socket); the server echoes the `bearer` subprotocol when it selected one.
- **Inbox confinement for agent-supplied reference clips (S2).**
  `AGENT_VOICE_ALLOW_ANY_PATH=false` is now the default: `clone_voice` over REST
  or MCP reads only `<DATA_DIR>/inbox/` (or `MCP_BASE_PATH`, when set) and a path
  escape is `403 PATH_OUTSIDE_BASE` before any ffmpeg probe. The CLI
  `agent-voice voices add` is an operator command and stays unrestricted.
- `server_info()` now reports `inbox_dir`, so an agent can see where a reference
  clip is allowed to come from without reading the configuration.

### Added

- `server_info().next_steps` — an advisory list of suggested next actions
  (clone a voice, install an engine, …). Empty when nothing needs doing; never
  an error.
- `SynthesisResult.realtime_multiple` — the canonical `duration_sec /
  elapsed_sec` ratio. `real_time_factor` remains as a deprecated alias for one
  release and is removed afterwards.
- `agent-voice doctor` — environment check: Python/`uv`, `ffmpeg`/`ffprobe`,
  `.env`, engine venvs, downloaded weights and daemon health, with a
  `PASS`/`FAIL`/`WARN` row per check and a non-zero exit when something required
  is broken.
- `agent-voice mcp-config --client <claude-code|claude-desktop|vscode|cursor|command-code>`
  — prints a ready-to-paste MCP client configuration for the current checkout,
  with absolute paths and `AGENT_VOICE_CLIENT_ID` filled in. Writes nothing.
- Documented platform support matrix: Linux + NVIDIA CUDA (verified), Windows
  (untested), macOS (mock engine only).
- Governance documents: `CONTRIBUTING.md`, `SECURITY.md`, this changelog, and
  YAML issue templates for bug reports and feature requests.

### Changed

- MCP tool schemas were made lighter (trimmed tool descriptions), and their error
  guidance kept actionable, so a tool call that cannot succeed explains what to do
  instead of failing opaquely.
- The README is now a ~250-line landing page; endpoint, configuration, engine,
  troubleshooting and client-installation detail moved to `docs/api.md`,
  `docs/config.md`, `docs/engines.md`, `docs/troubleshooting.md` and
  `docs/install/agent.md`.
- `pyproject.toml`: SPDX `license = "MIT"`, `keywords`, `classifiers`,
  `[project.urls]` (Homepage, Repository, Issues, Changelog), version `0.3.3`.

### Fixed

- The documented systemd user unit now sets
  `Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin`, so `ffmpeg`,
  `ffprobe` and `uv` resolve from the daemon's own environment instead of
  depending on the login shell's `PATH`.

## [0.3.2] — 2026-11-26 — `88c238a`

"Lexicon hotfix and production freeze."

### Fixed

- Pronunciation rewriting is Unicode-safe: replacement uses named regex groups,
  so a non-ASCII or numeric group name can no longer raise `KeyError`.
- `MAX_TEXT_CHARS` is enforced on the WebSocket `speak` frame, matching the HTTP
  route: the caller is rejected for what it sent, before any rewrite or worker
  spawn.

### Changed

- **Cache key:** the redundant lexicon-map hash was **removed**. The rewritten
  spoken text is the discriminator, so adding an unrelated pronunciation entry
  no longer invalidates existing renders; editing a term still invalidates only
  the lines that use it.
- The lexicon store is capped at 500 entries with 200-character keys and
  replacements; compiled patterns are cached, and an oversized per-call map is
  rejected with `422`/`BAD_REQUEST`.

## [0.3.1] — 2026-11-26 — `2e2a269`

"Parallel hardening, pronunciation lexicon, design candidates."

### Added

- Persistent pronunciation lexicon (`data/pronunciations.json`) with
  `pronunciations list|set|delete` and per-call `pronunciations={…}` overrides on
  `speak` and `speak_script`.
- `design_voice(candidates=1..5)`: render several takes in one call, save none,
  and register the chosen take from the cache afterwards.
- Admission caps `MAX_SCRIPT_BEATS`, `MAX_INSTRUCT_CHARS`,
  `MAX_DESIGN_SAMPLE_CHARS`, `MAX_REF_TEXT_CHARS`, enforced before routing and
  before any GPU time.
- `MAX_QUEUE_DEPTH` admission: a full global wait queue answers
  `429 BACKPRESSURE` instead of growing without bound. Concurrency tests cover
  20 clients and cache hits while the GPU is busy.
- Voice-name validation (`^[\w .\-]{1,64}$`) for `clone_voice`,
  `design_voice` and `voices add`.

### Changed

- **Cache key:** the pronunciation lexicon is hashed into the synthesis key, so
  a lexicon edit is a different render rather than a stale cache hit.
- `BeatSpec.quality` now inherits `ScriptRequest.quality`; a beat sets it only to
  differ from the script.
- `REQUIRE_ENGINE_APPROVAL` defaults to `true` in code as well as in
  `.env.example`.

### Fixed

- Oversized instruct/sample/ref text and over-long beat lists are rejected with
  a clean error body instead of reaching a worker.

## [0.3.0] — 2026-11-26 — `ec82927`

"Full Qwen3-TTS suite with voice design and preset speakers."

### Added

- Five official Qwen3-TTS 12Hz checkpoints: `1.7b-base`, `0.6b-base` (`clone`),
  `1.7b-custom`, `0.6b-custom` (`custom_voice`, nine preset speakers) and
  `1.7b-design` (`voice_design`).
- `mode` / `speaker` / `instruct` across REST, MCP and IPC, with routing that
  picks a checkpoint by capability instead of guessing.
- `design_voice` (core MCP tool, `POST /v1/voices/design`, `agent-voice voices
  design`) — describe a voice in words and optionally save the render as a clone.
- Preset-speaker routing, including `speaker=` inside `speak_script` beats and
  per-beat `instruct` on checkpoints that declare `supports_instruct`.
- OpenAI builtin-voice fallback: `alloy`/`echo`/`fable`/`onyx`/`nova`/`shimmer`
  resolve to a local voice or a preset speaker instead of `VOICE_NOT_FOUND`.

### Changed

- **Cache key:** `mode`, `speaker` and `instruct` became part of the synthesis
  key, so a preset timbre and a cloned voice can never share a cache entry.

## [0.2.1] — 2026-11-26 — `f93f4dd`

"Close v0.2 verification gaps and add Manim end-to-end demo."

### Added

- `examples/manim_demo/`: timed narration from `.timing.json`, rendered with
  Manim and muxed with ffmpeg; exits non-zero if audio and video drift by more
  than 750 ms.
- `engines test` now exercises the IPC `health` step (7 rows).
- Registry-owned `engines/.pending/` staging plus a reviewed
  `engines approve <id>` (prints model list and `worker.py` SHA-256, `--yes` for
  scripts).

### Fixed

- A non-ASCII `Authorization` header is a `401`, not a `500`: the bearer check
  compares UTF-8 bytes with `hmac.compare_digest`.
- ffmpeg/ffprobe failures surface the meaningful tail of stderr.
- `JobStore.prune` is wired to startup and the reaper; `testserver` removed from
  the production host allow-list.
- `delete_voice` moved into the core toolset and the toolset docs corrected.

## [0.2.0] — 2026-11-26 — `00b8b46`

"Security hardening, MCP excellence and voice-coding polish."

### Security

- `.env.example` defaults `ALLOW_AGENT_ENGINE_MUTATION=false`, with the
  code-execution consequence stated plainly.
- Host/Origin guard runs before auth: a foreign `Host` is `421`, a foreign
  `Origin` is `403`. Binding a non-loopback address without an API key refuses to
  start.
- `/audio` is behind the bearer token; artifact ids are validated as strict
  16-hex and the wildcard glob is gone, so `/audio/*` can no longer enumerate
  files.
- Bearer comparison uses `hmac.compare_digest`; mutations are appended to
  `data/mutations.log` with the `worker.py` SHA-256.
- MCP mutation tools enforce the same loopback rule as REST.

### Added

- MCP: `ToolError` failures (clients see `isError=true` with the
  `{error_code, message, suggestion}` JSON preserved), typed inputs/outputs,
  `ToolAnnotations` on every tool, a `core` vs `full` toolset, `server_info()`,
  the `agent-voice://voices` and `agent-voice://guide` resources, a
  `narrate_video` prompt, and per-beat job progress.
- Voice coding: `take=N` alternate deliveries, `download_url`, `chunks_used`,
  `elapsed_ms` and `real_time_factor` on `SynthesisResult`, and per-beat
  `cached`/`artifact_id`.
- `agent-voice cache stats|clear`, `agent-voice engines test <id>` (full IPC v1
  conformance) and `agent-voice engines pending|approve`.
- Durable job store: jobs interrupted by a restart reconcile to failed with
  `DAEMON_RESTARTED` and retain their record under `data/jobs/`.

### Changed

- **Cache key:** includes `take`, `hf_repo` and the resolved model revision, so
  re-pulling weights can never serve stale audio, and a new take never
  invalidates take 1.

### Fixed

- Jobs interrupted by a restart reconcile to failed instead of lying as
  "running".
- ASGI lifespan teardown no longer waits out a 10 s timeout, cutting the suite
  from ~4 minutes to 31 s.

## [0.1.0] — 2026-11-26 — `5f5224b`

Initial release.

### Added

- Headless, UI-free daemon with a core venv that never installs torch; engine
  code runs in isolated per-engine `uv` venvs, spawned lazily and reaped on idle.
- Multiplexed IPC v1 over Unix domain sockets (loopback TCP on Windows), with a
  dedicated worker reader thread so `cancel` and `health` land mid-decode.
- Three engines: `mock` (zero dependencies), `qwen3_tts` (official `qwen-tts`,
  emulated streaming) and `qwen3_tts_fast` (`faster-qwen3-tts` with CUDA graphs,
  native PCM streaming).
- 19 MCP tools returning compact JSON with structured
  `{error_code, message, suggestion}` failures, plus a token-free stdio shim
  that starts the daemon under a file lock.
- OpenAI-compatible `POST /v1/audio/speech`, agent endpoints, PCM streaming and
  a WebSocket with barge-in.
- Deterministic SHA-256 cache keyed on the full synthesis parameter set,
  consulted before any worker is spawned; long text chunked and stitched into one
  cached artifact.
- GPU lifecycle: `MAX_RESIDENT_MODELS` LRU eviction, `unload(force=…)` with
  `409 WORKER_BUSY`, and VRAM-pressure yield.
- `MCP_BASE_PATH` confinement for reference reads and artifact relocation.
- Every external command run as an argv list with `shell=False`.

[Unreleased]: https://github.com/000jd/agent-voice/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/000jd/agent-voice/compare/v0.3.3...v0.4.0
[0.3.3]: https://github.com/000jd/agent-voice/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/000jd/agent-voice/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/000jd/agent-voice/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/000jd/agent-voice/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/000jd/agent-voice/commit/f93f4dd
[0.2.0]: https://github.com/000jd/agent-voice/commit/00b8b46
[0.1.0]: https://github.com/000jd/agent-voice/commit/5f5224b
