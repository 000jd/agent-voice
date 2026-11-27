# AGENTS.md

`agent-voice` is a headless, UI-free speech server — synthesis, voice cloning, speech
recognition and two-stage dubbing — that an AI agent drives over MCP or HTTP. The agent asks
for speech or a transcript, gets back a file path or a PCM stream, and the GPU stays at 0 MB
the rest of the time.

## Architecture, in seven bullets

- **The core daemon never imports torch.** `src/agent_voice/` (FastAPI + MCP + CLI) holds no
  model code; that is a structural guarantee of the idle-GPU claim, not a convention.
- **Isolated per-engine venvs.** Each engine is a directory under `engines/<engine_id>/` with an
  `engine.yaml` manifest and a `worker.py`, installed into its own `uv` venv. Two engines that
  need mutually exclusive dependency sets coexist without conflict.
- **Six engines, two kinds.** `kind: tts` speaks, `kind: asr` listens, and the
  manifest declares which. Four TTS engines: `qwen3_tts_fast` (CUDA graphs) and
  `qwen3_tts` (official) on the GPU, `kokoro` (`kokoro==0.9.4`, CPU-only torch) and
  `mock` on the CPU. Two ASR engines: `phonon` (`fermion-research==0.2.15`, 25
  languages, 0 MB VRAM) and `mock_asr` (dependency-free, timed synthetic
  transcript). Kokoro is preset-timbre only (`voice_clone: false`,
  `voice_design: false`, `instruct_control: false`) and on its shipped `cpu` variant
  never initialises CUDA: `CUDA_VISIBLE_DEVICES=""` before the torch import,
  `device="cpu"` on `KPipeline`, `gpu_vram_mb: 0`.
- **`kind` routes, `device` schedules.** `kind` picks the IPC method
  (`synthesize`/`stream_pcm` vs `transcribe`) and the `engines test` table;
  `device: cpu` makes an engine **exempt from the single-GPU-owner slot**, so a
  transcription runs next to a resident TTS model instead of evicting it, and the
  VRAM-pressure reaper leaves it alone. Both default to the v0.4 behaviour (`tts`,
  `gpu`), which is why every older manifest still loads.
- **`device` schedules; `supported_devices`/`default_device` describe the backend.** They
  are different fields on purpose. Runtime devices are `cpu` · `cuda` · `mps` · `rocm`,
  resolved per engine as `AGENT_VOICE_DEVICES[engine]` → non-`auto`
  `AGENT_VOICE_DEVICE` → manifest `default_device` → host accelerator, and validated
  against `supported_devices`. `DEVICE_SUPPORT` in `src/agent_voice/hardware.py` is the
  support matrix and the only place a pair becomes `verified`; a device is a **dependency
  set** (each `uv.lock` bakes the torch index in), so switching one reinstalls that
  engine's venv via `engines install <id> --device <d> [--experimental]`. Only one
  variant per engine is resident, and the manager's torch probe turns a stale variant
  into a clear `409 ENGINE_NOT_INSTALLED`. The 0 MB claim belongs to `device=cpu`
  alone.
- **One home anchors every path.** `default_home()` is `AGENT_VOICE_HOME`, else the first
  parent holding both `pyproject.toml` and `engines/`, else `~/.agent-voice`.
  `get_settings()` reads `<home>/.env`, and relative `DATA_DIR`/`ENGINES_DIR` values
  resolve against the home — never the cwd — so the CLI and daemon work from any
  directory. Prefixed aliases (`AGENT_VOICE_DATA_DIR`, `AGENT_VOICE_ENGINES_DIR`,
  `AGENT_VOICE_MCP_BASE_PATH`) win over the unprefixed spellings. Do not reintroduce a
  `Path.cwd()` or `Path(__file__).parents[…]` anchor; `WorkingDirectory` no longer
  decides which `.env` is read.
- **Two surfaces per direction.** Speech out: `/v1/agent/speak`,
  `/v1/audio/speech` (OpenAI), the PCM stream and the WebSocket. Speech in:
  `/v1/agent/transcribe` (a confined local path) and `/v1/audio/transcriptions`
  (OpenAI multipart, bearer-protected by the `/v1` guard). The same two are MCP
  `transcribe()` and the `transcribe` CLI command.
- **Dubbing is two-stage and has no translation model.** `dub prepare` transcribes,
  extracts the bed and a speaker reference, and persists `project.json`;
  the *agent* translates the lines in its own context; `dub render` speaks them into
  their original slots, retimes each line up to `max_atempo`, and mixes. Bundling a
  translator would need a translation model, a tokenizer and a 2 GB download to do
  worse than the model already calling the tool.
- **Five Qwen checkpoints, three modes.** `engine.yaml` declares five Qwen3-TTS 12Hz aliases —
  two `clone` (`1.7b-base`, `0.6b-base`), two `custom_voice` (`1.7b-custom`, `0.6b-custom`, nine
  `preset_speakers`, `supports_instruct` on the 1.7B only) and one `voice_design`
  (`1.7b-design`). Routing picks a model by `mode`, so a `speaker=` request never lands on a
  checkpoint that can only clone. Kokoro adds one `custom_voice` model with eight voices.
- **IPC v1 is a frozen contract.** Length-prefixed, multiplexed frames over a Unix domain socket
  (loopback TCP on Windows). Correlation ids let `cancel` and `health` land while a decode is
  running. See `src/agent_voice/ipc/protocol.py` and `src/agent_voice/ipc/transport.py` — do not
  change the wire format. `mode`, `speaker` and `instruct` are **optional** fields on
  `synthesize`/`stream_pcm`; omitting them is exactly v1 behaviour, and a worker that does not
  understand them may ignore them. `transcribe` is additive in the same way: a TTS worker answers
  `BAD_REQUEST unknown method` and an ASR worker answers that to `synthesize`, so no framing
  change and no version bump were needed.
- **24 MCP tools** (16 core), gated into a `core` and a `full` surface (`AGENT_VOICE_TOOLSET`).
  The mutation tools that write and execute Python are hidden unless explicitly enabled.
  `transcribe`, `dub_prepare`, `dub_render` and `start_dub_job` are core, not full-only:
  speech in and dubbing are the feature, not an operator extra.
- **`skills/agent-voice/SKILL.md` is the playbook.** It is the agent-facing guide, and the
  `/mcp` `guide` resource serves it. Change agent-facing behaviour there, not in prose scattered
  through the README.

## Commands

```bash
# tests (mock engine only; needs no GPU and no torch)
.venv/bin/pytest -v                       # or: uv run pytest -v
.venv/bin/pytest -v -m "not gpu"          # explicit; same thing (addopts already excludes gpu)

# real GPU gate (opt-in, needs a CUDA GPU + downloaded weights)
AGENT_VOICE_RUN_GPU_TESTS=1 uv run pytest -v -m gpu -o addopts=""

# lint — must stay clean
.venv/bin/ruff check .

# end-to-end IPC v1 conformance against a live worker (~1 s, no GPU)
.venv/bin/agent-voice engines test mock
.venv/bin/agent-voice engines test mock_asr    # the ASR table: transcribe replaces synthesize
.venv/bin/agent-voice engines test kokoro     # CPU engine, needs the downloaded weights
.venv/bin/agent-voice engines test phonon     # CPU ASR engine, needs the downloaded weights

# environment + client onboarding
.venv/bin/agent-voice doctor
.venv/bin/agent-voice mcp-config --client claude-code

# hardware-aware install: detects the host, recommends verified pairs, writes .env,
# optionally downloads weights. --dry-run prints the plan and writes nothing;
# --yes alone NEVER downloads (only --download or an interactive yes does).
.venv/bin/agent-voice setup
.venv/bin/agent-voice setup --dry-run
.venv/bin/agent-voice setup -y --profile cpu-only --engines kokoro,phonon

# engine installs, including a specific device variant. Switching a device replaces
# that engine's venv in place; --experimental is required for an experimental pair
# and never unlocks an unsupported one. --dry-run prints the resolved project + argv.
.venv/bin/agent-voice engines install kokoro
.venv/bin/agent-voice engines install qwen3_tts --device cpu
.venv/bin/agent-voice engines install kokoro --device cuda --experimental
.venv/bin/agent-voice engines install qwen3_tts --device rocm --experimental --dry-run  # resolves variants/rocm (lock only; not run here)

# speech in: in-process, no daemon, no GPU (16 kHz WAV extraction happens once)
.venv/bin/agent-voice transcribe data/inbox/clip.wav --srt --words
.venv/bin/agent-voice transcribe data/inbox/clip.wav --hotwords "CUDA,Manim" --model phonon-2.5

# dubbing, two stages. The translation in the middle is YOUR job, not the daemon's.
.venv/bin/agent-voice dub prepare data/inbox/episode.mp4     # -> dub_id + timed segments
.venv/bin/agent-voice dub render <dub_id> segments.json --auto-clone-source

# design a voice from a description, in-process, no daemon needed
.venv/bin/agent-voice voices design narrator \
  --instruct "warm baritone, measured pace, late-night radio"

# the same path on the mock engine, which ships custom_voice and voice_design aliases
.venv/bin/agent-voice voices design probe --engine mock --model mock-design \
  --instruct "quiet and precise"

# pronunciation lexicon — global, project- and voice-scoped
.venv/bin/agent-voice pronunciations list
.venv/bin/agent-voice pronunciations set kubectl "cube control"
.venv/bin/agent-voice pronunciations set k8s "K eight S" --project infra
.venv/bin/agent-voice pronunciations delete kubectl
.venv/bin/agent-voice pronunciations show --project infra     # every layer + merged + hash
.venv/bin/agent-voice pronunciations check "HTTPServer v0.4.0" --spell-identifiers
```

`engines test <id>` is the fastest way to prove an engine still speaks protocol v1: it spawns a
real worker and walks `spawn+connect → hello → load_model → synthesize → cancel → health → shutdown`,
printing a pass/fail row per step and exiting non-zero on any failure. Use it after touching
anything in the worker, the transport or the manager. It picks a clone model first; an engine
with none (Kokoro) is exercised through its first `custom_voice` model's first preset speaker,
with `mode` and `speaker` sent on the synthesize and stream steps.

A `kind: asr` engine walks the sibling table — `spawn+connect → hello → load_model →
transcribe → cancel → health → shutdown` — because it has no `synthesize` to exercise. The
`transcribe` row sends a 1 s synthetic 16 kHz tone and passes on a well-formed reply (a list of
`segments` and a string `text`), never on words: a tone has none, and demanding them would fail a
healthy backend. The `cancel` row starts a **20 s** transcribe, waits until the worker itself
reports a request in flight, then cancels — and passes on `{cancelled: false}` too, because a CPU
decode can legitimately finish first. The detail string says which happened; never claim a cancel
that did not occur.

`pronunciations check <text-or-file>` is the read-only prediction of the rewrite pipeline
(lexicon terms with hit counts, unknown ALL-CAPS acronyms, and — with `--spell-identifiers` —
code-like tokens with their `[spell]` and code-mode readings). It reads a file when the
argument is one, never synthesises, and is the cheapest way to debug "why did it say that".

`voices design` is the same in-process pattern the conformance test and the Manim demo use: build
an `AppContext`, `await ctx.workers.start()`, call the service, `await ctx.workers.stop()` inside
`asyncio.run`. No running daemon is involved, which is why every heavy import stays inside the
command body. `--candidates 2..5` renders several takes to audition and saves none; re-run with
`--take <n> --save-as-clone` (no `--seed`) to register the winner from the cache.

## Security defaults

- **Engine mutation is off by default.** `ALLOW_AGENT_ENGINE_MUTATION=false`. Enabling it lets an
  AI agent write `engine.yaml` / `worker.py` / `pyproject.toml` into `engines/` *and execute it*,
  because `worker.py` is run as a Python script. That is arbitrary code execution requested by a
  language model; leave it off unless you specifically want that.
- **Loopback only.** Even with mutation enabled, both the daemon and the calling client must be on
  loopback. A non-loopback peer gets `AGENT_MUTATION_DISABLED`.
- **Loopback by default end to end.** The server binds `127.0.0.1`, a pure ASGI Host/Origin guard
  rejects DNS-rebinding attempts on HTTP **and** WebSocket handshakes, and `/audio` sits behind
  the bearer token because an artifact is the user's own speech. Set `AGENT_VOICE_API_KEY` before
  exposing anything. A whitespace-only key counts as unset. A WebSocket that fails the guard is
  refused **before** `accept()` — that is an **HTTP 403** over real uvicorn, not a `1008` close
  (only Starlette's TestClient fabricates 1008); browser clients may carry the key in
  `Sec-WebSocket-Protocol: bearer, <key>` and the server echoes the matched spelling.
  `agent-voice serve --host 0.0.0.0` validates the effective bind, so a non-loopback address
  without a key refuses to start whatever `.env` says.
- **Agent clone paths are confined.** `AGENT_VOICE_ALLOW_ANY_PATH=false` (default): `clone_voice`
  over REST/MCP reads only `<DATA_DIR>/inbox/` unless `MCP_BASE_PATH` is set. A rejected path is
  `PATH_OUTSIDE_BASE` before any ffmpeg probe, and MCP and REST share one resolver. The CLI
  `voices add` is a human operator path and remains unrestricted. **Transcription reads through
  the same resolver** (`resolve_media_path` → `resolve_ref_audio_path`), so `transcribe` and
  `dub_prepare` cannot read a file a clone could not.
- **Input caps are admission control.** `MAX_SCRIPT_BEATS=200`, `MAX_INSTRUCT_CHARS=1000`,
  `MAX_DESIGN_SAMPLE_CHARS=500` and `MAX_REF_TEXT_CHARS=2000` are checked before routing and
  before a worker is spawned, so an oversized request costs a round-trip and no GPU time.
  Oversized text is `413 TEXT_TOO_LONG`; an over-long beat list is `BAD_REQUEST`. Markup
  (128 tags, depth 4, 32 segments) and lexicon (500 per scope, 1,500 merged) caps are checked at
  the same point. A new cap belongs at admission too, never after the expensive part.
  `MAX_TRANSCRIBE_SECONDS` (3600) is a cap on **work**, not on upload size: it is checked against
  the ffprobe duration before any extraction, and an over-long clip is `BAD_REQUEST` naming the
  cap. `MAX_DUB_SEGMENTS` (500) bounds one render and is checked before any synthesis.
- **Lexicon caps are a regex bound.** A merged map is validated on load and again before
  `_compiled`, so a hand-edited file or a raw WS/OpenAI map cannot compile an unbounded
  alternation.
- **The hotword cap is a decoder bound.** `MAX_HOTWORDS = 25` is a contract constant in
  `schemas.py`, not an env knob: an unbounded bias list is unbounded decoder work, the same
  reasoning that keeps the lexicon caps structural.
- `REQUIRE_ENGINE_APPROVAL` defaults to `true` in code as well as in `.env.example`: an
  agent-authored plugin is staged under `engines/.pending/<id>/` until an operator promotes
  it with `agent-voice engines approve <id>`.
- **Optional tools are WARNs, not FAILs.** A missing `espeak-ng` degrades Kokoro's
  out-of-dictionary words only, so `doctor` reports it as a WARN with the platform install
  command. A missing GPU is a WARN too; only a broken required tool (`ffmpeg`, `uv`, a bad port)
  is a FAIL, and only a FAIL exits non-zero. The `asr` row (default ASR engine not installed) and
  the `dub filters` row (this ffmpeg lacks `sidechaincompress`/`alimiter`) are both WARNs — the
  first because a TTS-only box is a supported setup, the second because a dub still completes with
  a plain mix.

## Error taxonomy

One taxonomy across IPC, REST and MCP, defined in `ErrorCode` in
`src/agent_voice/schemas.py`. Every error is `{error_code, message, suggestion}` — never a bare
string, never a stack trace. `HTTP_STATUS_BY_ERROR` in the same file maps codes to status codes;
`agent-voice status`, the REST routes and the MCP tools all render from it. Add a code there, not
at a call site.

## Do not change `httpx2`

`httpx2` is required by `fastmcp 4.2.6`. It is not a typo for `httpx`, and dropping it,
"correcting" it to `httpx`, or pinning it away will break the MCP transport with an import error
that does not name the real cause. Both `httpx` (for the CLI's REST calls) and `httpx2` (for
fastmcp) are legitimately present.

## House rules

- Every subprocess uses an argv list with `shell=False`. There is no path in this project that
  builds a shell string, and none should be added.
- Cache hits and read-only calls (`server_info`, `gpu_status`, `list_voices`) never wait behind
  the GPU lock. Uncached jobs queue FIFO per engine, and `MAX_QUEUE_DEPTH=64` turns a full wait
  queue into `429 BACKPRESSURE` instead of unbounded latency. `MAX_QUEUE_DEPTH` is **global**,
  not a per-engine budget.
- `qwen3_tts_fast` stays strictly serialized: its CUDA graphs are captured at batch 1, so there
  is no micro-batching to add there. Throughput comes from the cache and the queue.
- **The rewrite order is structural, not stylistic:** parse markup on the *raw* text →
  `resolve_target` (the voice scope needs the resolved `voice_id`) → scoped lexicon → code
  normalization on the lexicon's complement → per-segment synthesis. Lexicon first means an
  operator mapping always wins over a heuristic, and `[spell]` spans are protected from both.
  Do not reorder these steps at a call site; go through `services/utterance_service.py`.
- The rewritten spoken text is what the synthesis cache keys on: a lexicon edit invalidates
  only lines that use the changed term, and unrelated entries leave old renders cached.
  Subtitles keep the original written text (`display_text`), never the spoken form.
- **`CACHE_KEY_FIELDS` does not change for markup.** `markup`, `spell_identifiers`,
  `paragraph_gap_ms` and `project` are not key fields; the rewritten text and effective `speed`
  are, pauses are local silence, `[emphasis]` gain is applied after the segment cache, and
  `pronunciation_hash` is observability only.
- Typer sub-commands must not import torch. Defer heavy imports into command bodies.
- `pyproject.toml`'s core `dependencies` must never gain torch.
- **`device: cpu` is a scheduling promise, not a label.** `_begin_job` must not acquire the GPU
  slot for a `cpu` engine, and the VRAM-pressure reaper must never kill one. If a new code path
  starts a worker, it inherits that rule from `_begin_job` — do not re-check the manifest in three
  places and get two of them wrong.
- **`DEVICE_SUPPORT` is evidence-only.** A `(engine, device)` pair becomes `verified` only when a
  measured run is recorded in `docs/phase0-devices-report.md` (or a named prior report); everything
  else is `experimental` or `unsupported`, and no doc, README row or `doctor` line may round that
  up. Never invent a speed multiplier — "slow on CPU" is what the measurement supports
  (0.196–0.22× realtime, 5.55 GiB peak RSS), and an unsupported pair stays refused even with
  `--experimental`.
- **The core stays torch-free, including the device paths.** `hardware.py`, `setup_wizard.py`,
  `config.py` and `cli.py` must not import torch: hardware probes are `subprocess` argv lists or
  stdlib reads, and the only torch inspection goes through an engine's own interpreter
  (`engine_registry._torch_probe`).
- **`kind` decides the method, never the caller.** Routing checks it in exactly two places —
  `resolve_target` (refuses an ASR engine for speech) and `resolve_asr_target` (refuses a TTS
  engine for transcription) — so a wrong-kind request is a clear `BAD_REQUEST`, not a worker crash.
- All six engines are plugins. A core change that names one engine id (`kokoro`, `phonon`, …) is a
  bug unless the id is a test fixture; capability questions go through the manifest.
