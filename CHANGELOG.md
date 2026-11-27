# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Dates are the commit dates (`git log --format='%h %cs %s'`); short hashes are
given for every release so a tag can be verified against the history.

## [Unreleased]

No unreleased changes.

## [0.6.0] — 2026-11-27 — `<commit>`

"Install in one command, let setup detect the hardware, and switch an engine between
CPU, CUDA, MPS and ROCm — with a support matrix that says which pairs were actually
run."

### Added

- **One-command installers.** `scripts/install.sh` (POSIX `sh`) and
  `scripts/install.ps1` (PowerShell, **experimental/unverified on real Windows**):

  ```bash
  curl -fsSL https://raw.githubusercontent.com/000jd/agent-voice/main/scripts/install.sh | sh
  irm https://raw.githubusercontent.com/000jd/agent-voice/main/scripts/install.ps1 | iex
  ```

  The default version is the **latest release tag**, resolved with
  `git ls-remote --tags --refs` and semver-sorted; `main` is installed only for an
  explicit `--ref main`. Flags: `--dir`, `--version`, `--ref`, `--repo`, `--yes`,
  `--profile`, `--download`/`--no-download`, `--no-setup`, `--uninstall`, `--dry-run`,
  `--help` (PowerShell mirrors them as `-Dir`, `-Version`, `-Ref`, `-Repo`, `-Yes`,
  `-Profile`, `-Download`, `-NoDownload`, `-NoSetup`, `-Uninstall`, `-DryRun`). `uv` is
  installed only if missing and only with consent; no `sudo`, no telemetry; every
  subprocess is an argv list. `--dry-run` prints every action and `argv` and writes
  nothing. **The data directory is a sibling of the install directory**
  (`~/.local/share/agent-voice-data`, `%LOCALAPPDATA%\agent-voice-data`) and
  `--uninstall` never removes it, so re-installing costs no voices, lexicons, cache or
  weights. Docs: `docs/install/one-command.md`.
- **`agent-voice setup`** — a hardware-aware wizard with a pure planning core
  (`setup_wizard.py`) and injected `Runner`/`Downloader`. Flags: `-y/--yes`,
  `--non-interactive`, `--profile auto|cpu-only|nvidia-gpu|apple-silicon`,
  `--device auto|cpu|cuda|mps|rocm`, `--engines <csv>`, `--download`/`--no-download`,
  `--dry-run`. Interactive only on a real TTY (and not for `--yes`,
  `--non-interactive` or `--dry-run`); with no terminal and no `--yes` it prints the
  plan, applies nothing and exits non-zero telling you to re-run with `--yes`.
  **`--yes` alone never downloads weights** — only an explicit `--download` (or an
  interactive yes) does. It writes `<home>/.env` atomically with
  mode `0600`, preserving comments and unknown keys, after backing the old file up to
  `.env.bak` (or `.env.<UTC>.bak` if a backup exists — an existing backup is never
  overwritten), records **absolute** `DATA_DIR`/`ENGINES_DIR`/`AGENT_VOICE_HOME` plus the
  device and default-engine settings, never echoes `AGENT_VOICE_API_KEY`, then runs
  `doctor` and prints the `mcp-config` snippet and the per-pair support table.
- **Hardware detection** (`src/agent_voice/hardware.py`) — zero torch, zero engine
  imports, every OS/GPU branch injectable. NVIDIA via `nvidia-smi` (argv list,
  `shell=False`, timeout), AMD via `rocm-smi`/`rocminfo`, Apple silicon via
  `sys.platform`/`platform.machine`, RAM via `/proc/meminfo`, `sysctl` or
  `GlobalMemoryStatusEx`, tools via `shutil.which`. `DEVICE_SUPPORT` is the single
  support matrix; `recommend_profile()` uses **only verified pairs as defaults**, with
  thresholds that agree with `MAX_GPU_VRAM_GB=12.5` / `MIN_FREE_VRAM_GB=2.0`. The
  measurements behind every row are in `docs/phase0-devices-report.md`.
- **Device preference and switching.** `AGENT_VOICE_DEVICE=auto|cpu|cuda|mps|rocm` and
  per-engine `AGENT_VOICE_DEVICES=kokoro=cpu,qwen3_tts=cuda`, resolved as
  per-engine override → non-`auto` global → manifest `default_device` → host
  accelerator, and validated against the manifest's `supported_devices` with an error
  that names the engine, the device and the supported list. `engines install <id>
  --device <d>` (and its `--experimental` gate) selects a variant project root under
  `engines/<id>/variants/<device>/`, syncs it with `uv sync --project … --locked` and
  `UV_PROJECT_ENVIRONMENT=<engine>/.venv`, and writes an `.variant` stamp (device +
  torch version) next to the venv. `mock` and `kokoro` now declare `device: cpu`, so they
  no longer take the single-GPU-owner slot while holding 0 MB. Only one variant per engine
  is resident; switching replaces that engine's venv in place.
- **Per-engine device plumbing.** Every worker takes the resolved device, reports it in
  `hello`/`health`/`unload_model`, and reports real VRAM on cuda/rocm/mps instead of a
  hard-coded 0. `server_info()` gains `device`/`devices`; `gpu_status()` gains
  `cpu_engines[]`, `worker_devices`, `worker_vram_mb` and `vram_source`
  (`nvml`|`worker`|`none`). The synthesis cache key includes the resolved device — a CPU
  and a CUDA render are not bit-identical.
- **Home anchoring.** `default_home()` is `AGENT_VOICE_HOME`, else the checkout that
  holds both `pyproject.toml` and `engines/`, else `~/.agent-voice`; `get_settings()`
  constructs `Settings(_env_file=home / ".env")`, so the daemon reads `<home>/.env`
  rather than `$PWD/.env`. `Settings.home` exists, `data_dir`/`engines_dir` resolve
  relative values against the home, `config_path`/`home` joined `SettingsLike`, and
  `AGENT_VOICE_DATA_DIR` / `AGENT_VOICE_ENGINES_DIR` / `AGENT_VOICE_MCP_BASE_PATH` are
  accepted as prefixed aliases (the `AGENT_VOICE_*` spelling wins). `mcp-config` uses
  `default_home()` instead of `Path(__file__).parents[2]`, and the stdio proxy passes
  `cwd=<home>` plus `AGENT_VOICE_HOME` to the daemon it spawns.
- **`doctor` severity `INFO`.** Uninstalled engines and undownloaded model aliases that
  you did **not** select are `INFO` (rank 1) instead of noise; a missing accelerator is
  `INFO` because a CPU box is supported; selected-but-missing stays `WARN`; only `FAIL`
  changes the exit code. New `hardware` row (OS/arch/cores/RAM/accelerator), `home` row
  (resolved home/data/engines/config) and `device` row (per installed engine, resolved
  device plus `verified`/`experimental`). The summary reads
  `N checks, X optional, Y warning(s), no failures`.
- **Device variants**: `qwen3_tts` ships `cpu`, `mps` and `rocm` variant projects under
  `variants/` (canonical project = `cuda`); `kokoro` ships `cuda` and `mps` (canonical =
  `cpu`); `phonon` ships `mps` (Fermion's `mlx` extra, canonical = `cpu`). Each has its
  own `pyproject.toml`, `uv.lock` and `[tool.agent-voice] device`. The canonical
  committed `uv.lock` is never rewritten — verified byte-for-byte (same sha256 *and*
  mtime). The ROCm and MPS locks were produced on this Linux host; those pairs stay
  `experimental` because nothing was run on AMD or Apple silicon. An
  `engines install <id> --device <d>` for a supported pair with no shipped variant
  project is refused with the list of variants the checkout actually has.
- **Documentation of the honest matrix.** The README and `docs/engines.md` now carry the
  same per-`(engine, device)` table as `hardware.py:DEVICE_SUPPORT`, with
  `verified here` / `experimental` / `unsupported` and the measured evidence for each
  row. `docs/config.md` gains a Hardware section and the `[PATH]` table with the alias
  spellings; `docs/troubleshooting.md` gains the stale-variant 409, the `VIRTUAL_ENV`
  warning, the no-variant and wrong-device cases, the interrupted-installer and
  `AGENT_VOICE_HOME` sections; `.env.example` gains `# --- Hardware ---` and the home
  notes.

### Changed

- **A device is a dependency set, not a runtime flag.** Each committed `uv.lock` bakes
  the torch index into every torch entry, so CPU and CUDA lock different torch builds
  (`2.15.0+cpu`/`2.11.1+cpu` versus `2.11.0+cu128`/`2.11.0+cu128`). `engines install` now
  always passes `--locked`: without it, a `uv sync` against a drifted variant silently
  rewrites the committed lock (measured: the sha changed and a package was added).
- **Scheduling uses the resolved device.** `_begin_job`, `ensure_worker` and the reaper
  reason about the resolved device class (cpu versus accelerator, with MPS/ROCm in the
  accelerator class) rather than the static `device` field, so a `kokoro` switched to
  `cuda` takes an accelerator slot and a `qwen3_tts` switched to `cpu` does not.
- **OOM and death classification is device-aware**: `MPS backend out of memory`,
  `hipErrorOutOfMemory` and `DefaultCPUAllocator` are recognised, and `CUDA_OOM` is only
  used for cuda/rocm.
- **`Settings` reads no `.env` by default.** `model_config` is `env_file=None` and
  `get_settings()` supplies `<home>/.env`, so a direct `Settings(...)` in a test is
  hermetic; `_env_file=None` remains the escape hatch.
- **README install order.** The one-command installers come first and the `git clone`
  from-source path is a secondary section; both end at `agent-voice setup` or the
  per-engine commands. Platform support is now a per-device matrix rather than three
  rows.
- `pyproject.toml` and `src/agent_voice/__init__.py`: version `0.6.0`. No dependency
  changes; the core venv still installs no torch.

### Security

- **`SECURITY.md` gains "Supply chain and the one-command installer"**: what each
  installer fetches, tag pinning by default, `--dry-run` as the audit path, no `sudo`,
  no telemetry, argv-list subprocesses, the data directory living outside the install
  directory, and the inherent `curl | sh` trust decision with the reviewed-clone,
  `--repo <local path>` and from-source alternatives. The Supported versions table is
  current again (`0.6.x` / `0.5.x` / `≤0.4.x`).
- **`engines install` cannot be tricked into an index override.** It never passes
  `--index`, `--default-index`, `--index-url`, `UV_INDEX` or `UV_DEFAULT_INDEX` and
  strips them from the environment: measured, `[tool.uv.sources]` wins over all of them,
  and on the real CUDA project `--index` silently downgraded torch to `2.9.1+cu128`
  while leaving torchaudio at `2.11.0+cu128` — a mismatched pair with a plausible-looking
  venv.
- **The setup `.env` writer is conservative.** It updates only known keys, preserves
  comments and unknown keys byte-for-byte, writes atomically, uses mode `0600`, never
  overwrites an existing backup, and never echoes the API key.
- **`AGENT_VOICE_RESOLVED_DEVICE` is always overwritten** into a worker's environment
  (never `setdefault`), so an inherited value cannot silently change the backend, and a
  worker that reports a different device than it was given is a clear error rather than
  a quiet downgrade.

### Fixed

- **`resolve_target`/`resolve_asr_target` check the manifest `kind` before the install
  gate**, so on a fresh clone `speak(engine="phonon")` says "cannot speak" instead of
  "install phonon" — the actionable error, first time.
- **`PATH_OUTSIDE_BASE` names the real base.** The inbox case says
  `path escapes the inbox (<inbox_dir>): <path>` instead of always claiming
  `MCP_BASE_PATH`; the MCP-confined message stays byte-identical.
- **`Settings(api_key="x")` is no longer silently dropped**, without letting the
  standard `HOME`/`PORT`/`HOST`/`DEVICE` environment variables match fields. The home is
  an `agent_home` field behind a `home` property, and constructor field names are mapped
  to their aliases explicitly; `Settings(_env_file=None).home` is the checkout, not
  `$HOME`.
- **`auto` never silently chooses an `experimental` device when a verified one exists.**
  On Apple, `qwen3_tts` (manifest default `cuda`) resolves to the verified CPU path, not
  experimental MPS; an explicit `AGENT_VOICE_DEVICE=mps` still opts in.
- **`GET /v1/engines` and MCP `list_engines` fill `resolved_device`** from the live
  workers instead of always returning `null`.
- **`pytest` works from the bare console script** without `PYTHONPATH`:
  `[tool.pytest.ini_options] pythonpath = ["."]`, and the `PYTHONPATH` workaround is gone
  from CI and the docs.
- The MCP `_unwrap` guard no longer mangles a digit-only payload string into a number.
- `doctor`'s `.env` probe reads `settings.config_path` (`<home>/.env`), and its fix
  suggests `agent-voice setup`.
- `kokoro` and `mock` declared no `device:`, so they inherited `gpu` and took the
  single-GPU-owner slot while holding 0 MB. Both now declare `device: cpu`.
- `scripts/install.sh` no longer duplicates its managed `.env` banner on a re-run, and
  `setup` keeps the actionable suggestion when an engine variant is missing.

### Known limitations

- **`qwen3_tts` on CPU is slow, and the number is measured, not estimated**: 0.196–0.22×
  realtime (about five times slower than realtime) and 5.55 GiB peak RSS for a 0.6B
  checkpoint on a Ryzen 7 5700X. No speed multiplier is invented anywhere; use it for
  short utterances when its voice matters.
- **Only the shipped variants are installable.** `qwen3_tts` offers `cuda` (canonical),
  `cpu`, `mps` and `rocm`; `kokoro` offers `cpu` (canonical), `cuda` and `mps`; `phonon`
  offers `cpu` (canonical) and `mps`. The `(qwen3_tts, rocm)` and `(qwen3_tts, mps)`
  pairs are `experimental` — the locks were produced, but nothing was run on AMD or Apple
  silicon. `--experimental` is required; an unsupported pair is refused regardless.
- **Only one device variant per engine can be resident.** The venv is the canonical
  `engines/<id>/.venv`, and switching replaces its contents. On a cold `uv` cache that
  is a full download: the CPU venv is ~1.6 GB, the CUDA venv ~7.3 GB.
- **MPS and ROCm are `experimental`.** Both locks are producible from Linux, and nothing
  was installed or run on Apple silicon or an AMD GPU. `recommend_profile` never selects
  them; `engines install` requires `--experimental`.
- **Unsupported pairs stay refused**, `--experimental` or not: `qwen3_tts_fast` is
  CUDA-only (no ROCm or HIP claim exists in the distribution), and `phonon`'s NVIDIA
  path is the `ghcr.io/fermionresearch/phonon-cuda` container, not a wheel.
- **The 0 MB VRAM claim holds only for `device=cpu`.** The `kokoro`/`phonon` cuda/mps
  paths report real memory and are experimental; the existing 0 MB assertions stay on
  the CPU path.
- **Windows is unverified**, including `scripts/install.ps1`, which is parse-checked at
  best. Treat both as experimental.
- **Intel macOS is not a supported Phonon host**: `fermion` pins `torch>=2.2,<2.3`
  there, which cannot coexist with the engine's `torch==2.15.0` CPU pin.
- `models download --no-wait` is still accepted but ignored (L1, unchanged).

## [0.5.0] — 2026-11-27 — `0d4d31d`

"Speech in: pluggable ASR engines, agent-native two-stage dubbing, and a CPU
listener that never touches the GPU."

### Added

- **`kind: tts | asr` on the engine manifest** (default `tts`), so speech
  recognition is a family of plugins rather than a core feature. Every v0.4
  manifest keeps loading unchanged. `kind` decides the IPC method
  (`synthesize`/`stream_pcm` versus `transcribe`) and the `engines test` table;
  `resolve_target` and `resolve_asr_target` each refuse the wrong kind with
  `BAD_REQUEST` before any worker spawns.
- **`transcribe` IPC method** (protocol v1, additive): request
  `{model_alias, audio_path, language, word_timestamps, hotwords}`, response
  `{text, language, duration_sec, truncated, segments[{index, start_sec, end_sec,
  text, words?}]}`. No framing change and no version bump: a TTS worker answers
  `BAD_REQUEST unknown method` and an ASR worker answers that to `synthesize`.
  Word timings are returned **only** when `word_timestamps` is true.
- **`phonon` ASR engine** (`engines/phonon/`, `fermion-research==0.2.15`,
  CPU-only torch from the `pytorch-cpu` index): Phonon-2.5, 25 languages,
  **0 MB VRAM**, 156 MB download. Measured **≈42–48× real time** on a Ryzen 7
  5700X with **1.0 GB** peak RSS and a 1.05 s warm load; the first load is 50 s
  (download + SHA-256 verification + unpack). Three aliases: `phonon-2.5`
  (default), `phonon-2.5-en`, `phonon-2` (`fast`). The spike is recorded in
  `docs/phase0-asr-report.md`.
- **`mock_asr` engine**: dependency-free, reads a 16 kHz WAV with the stdlib,
  emits deterministic timed segments and honours `cancel`. The reference for the
  `kind: asr` shape; `agent-voice engines test mock_asr` needs no GPU and no deps.
- **Transcription surface**: `POST /v1/audio/transcriptions` (OpenAI multipart,
  `json`/`text`/`srt`/`vtt`/`verbose_json`, bearer-protected by the `/v1` guard),
  `POST /v1/agent/transcribe` (a confined local path), MCP `transcribe()`, and
  `agent-voice transcribe <file> [--engine] [--model] [--language] [--hotwords]
  [--words] [--srt] [--project]` — the last one in-process, no daemon.
- **Two-stage dubbing with no bundled translation model**: `dub prepare`
  transcribes, keeps the timing, extracts a 24 kHz bed and a 5–10 s speaker
  reference; the *caller* translates in its own context; `dub render` speaks the
  lines into their original slots, retimes each with `atempo` up to
  `max_atempo`, ducks the bed with `sidechaincompress` + `alimiter` and muxes the
  video (`-c:v copy` unless `burn_subtitles`). A line that still overruns is
  reported in `overflow_warnings`, never truncated. Surfaces:
  `POST /v1/agent/dub/prepare`, `/v1/agent/dub/render`, `/v1/agent/dub/jobs`
  (`Job.kind == "dub"`), MCP `dub_prepare`/`dub_render`/`start_dub_job` plus the
  `dub_video` prompt, and `agent-voice dub prepare|render`.
- **ASR-aware `engines test`**: `ASR_STEP_NAMES` is the sibling table
  (`spawn+connect, hello, load_model, transcribe, cancel, health, shutdown`), and
  `_skip_rest` now takes the table as a keyword argument instead of reading the
  module global. The `transcribe` row sends a 1 s synthetic 16 kHz tone and passes
  on a well-formed reply, never on words. The ASR `cancel` row starts a 20 s
  transcribe, waits for the worker to report a request in flight, then cancels —
  and passes on `{cancelled: false}` too, because a CPU decode can legitimately
  finish first. The detail string records which happened.
- **`doctor` rows for the new surfaces**: `asr` (the configured ASR engine and its
  aliases, or a WARN naming `engines install <id>`) and `dub filters`
  (`sidechaincompress`/`alimiter` present). Both WARN-only — a TTS-only host and a
  plain-mix dub are supported setups.
- `AGENT_VOICE_DEFAULT_ASR_ENGINE` (default `phonon`), `MAX_TRANSCRIBE_SECONDS`
  (3600, a cap on work checked before extraction), `MAX_DUB_SEGMENTS` (500,
  checked before synthesis), `data/dubs/<dub_id>/` in the DATA_DIR layout, and
  `MAX_HOTWORDS = 25` as a structural contract constant.
- MCP: `server_info()` gains `default_asr_engine`, `asr_engines[]` and
  `dubbing_available`; `gpu_status()` gains `cpu_engines[]`.
- `engines/ADDING_AN_ENGINE.md` gains a full **"Adding an ASR engine"** section:
  the `transcribe` shape, the three segment-grouping rules (sentence punctuation,
  a pause ≥ 0.8 s, a 7 s cap), the hotword cap, and a worked
  `engines/faster_whisper/` sketch with **zero core daemon edits**.

### Changed

- **Behaviour change — `device: cpu` engines are exempt from the GPU owner slot.**
  `_begin_job` acquires the GPU only for `device: gpu`; the VRAM-pressure reaper
  skips CPU workers. **Kokoro now declares `cpu`**, so a Kokoro render no longer
  takes the single-GPU-owner slot: it no longer evicts, and is no longer evicted
  by, a resident Qwen model. `phonon` lands in the same bucket. Anyone who relied
  on Kokoro serialising against a GPU engine will see them run concurrently; both
  still hold 0 MB of VRAM.
- **`engines test` is kind-aware.** A `kind: asr` engine walks the transcription
  table; a `tts` engine keeps the exact v0.4 sequence. Exit-code contract
  unchanged: any `ok is not True` (after the existing SKIP semantics) still exits 1.
- **`_conformance_target` returns a step kind as its first element** (`"transcribe"`
  or a model mode) rather than only a mode, which is what lets one function pick
  the compute step for both families.
- **Path confinement covers transcription.** `resolve_media_path` is
  `resolve_ref_audio_path` under a different field noun, so `transcribe` and
  `dub_prepare` read exactly where `clone_voice` may — `MCP_BASE_PATH`, then the
  inbox, then `AGENT_VOICE_ALLOW_ANY_PATH`. A `403 PATH_OUTSIDE_BASE` is raised
  before any ffmpeg work.
- Docs: the two inbox-confinement gaps from the v0.4 audit are fixed — the
  `README.md` security section now states `MCP_BASE_PATH` precedence and names
  `403 PATH_OUTSIDE_BASE` for transcription input, and the Manim demo entry notes
  that a real-engine run needs a registered voice. `SKILL.md` gained transcription
  and dubbing sections, an `inbox_dir` bullet in the `server_info()` key list, an
  `inbox_dir`-anchored `clone_voice` example, `PATH_OUTSIDE_BASE` /
  `INVALID_REF_AUDIO` rows in the error table and the operator escape-hatch
  paragraph. `docs/api.md` documents the five new endpoints and the new caps,
  `docs/config.md` the three new variables and `data/dubs/`,
  `docs/troubleshooting.md` the Phonon install, the missing
  `sidechaincompress` and `PATH_OUTSIDE_BASE` on a transcription.
- `pyproject.toml`: version `0.5.0`, `asr` / `speech-to-text` / `phonon` keywords.
  No dependency changes; the core venv still installs no torch.
- Engine count in `AGENTS.md` corrected to **six** (four TTS + two ASR), and the
  MCP tool count to **24** (16 core) now that transcription and dubbing are core.

### Security

- Transcription input is confined by the **same** resolver as a cloning reference
  clip, so reading a file for a transcript cannot become a hole in the inbox
  boundary. `PATH_OUTSIDE_BASE` is raised before the file is opened.
- `hotwords` is capped at **25** terms of at most 64 characters each, in the
  schema, before a worker sees it — the same structural reasoning that caps the
  lexicon: an unbounded bias list is unbounded decoder work.
- The OpenAI multipart route takes **only the suffix** from a client filename and
  generates the rest, so `../../cron.d/x` cannot choose where the bytes land. The
  upload is streamed 1 MB at a time and deleted in a `finally`.
- `MAX_TRANSCRIBE_SECONDS` and `MAX_DUB_SEGMENTS` are admission checks: an
  over-long clip costs one ffprobe, and an over-long render is refused before the
  first line is synthesised.

### Fixed

- The `VOICE_REQUIRED` suggestion names `settings.inbox_dir` instead of a
  placeholder path, so an agent that has never read the config still gets a path
  it can use.
- `README.md` no longer implies the Manim demo works end to end on a real engine
  without a registered voice.
- `agent-voice transcribe --project <slug>` now actually applies the project
  lexicon's terms as hotwords. The flag was validated and then dropped before
  the service call, so the documented scoped-lexicon path was a no-op.
- `agent-voice models download` now asks the backend-aware
  `weights_downloaded` predicate. A Fermion model is a packed `.fsq` archive,
  not a `.safetensors` file, so the old HF-cache check re-ran
  `snapshot_download` on every invocation while `doctor` correctly reported the
  model present.
- `mock_asr` reports its one fixed language (`English`) instead of echoing a
  requested language it cannot recognise; the manifest declares
  `multilingual: false` and the reply must agree with it.
- The Phonon worker forces `FERMION_DEVICE=cpu` (not `setdefault`), so an
  operator's stray `FERMION_DEVICE=cuda`/`mlx` cannot silently defeat the
  zero-VRAM promise. It also retries a transcription without `hotword_lambda`
  if the installed backend rejects that optional kwarg — the Phase 0 spike
  measured `hotwords=` only — and segment grouping now closes on `done."` /
  `really?)` rather than only a bare period.
- `engines/phonon/pyproject.toml` limits uv's resolution environments to Linux,
  Windows and Apple silicon: Fermion pins `torch>=2.2,<2.3` on Intel macOS,
  which cannot coexist with the engine's `torch==2.15.0` CPU pin. Without the
  limit, `uv sync` failed before reaching the host.
- `dub_render` now reports `bed_ducked`, refuses a blank translated line before
  any synthesis, writes the translated `segments[]` into `.timing.json` (which
  the docs already promised), and names the deliverable
  `<artifacts>/dub_<dub_id>.mp4`.

### Known limitations

- Phonon-2.5 **auto-detects** the language and does not report which one it chose,
  so `TranscribeResult.language` is `"auto"` for it. There is no per-request
  language control on that checkpoint; `phonon-2.5-en` is the fixed-English one.
- The Phonon result carries **no confidence field**, so none is exposed. Do not
  read confidence into a transcript.
- A prepared-but-never-rendered dub is kept under `data/dubs/<dub_id>/` on
  purpose (so a translation session can span a restart) and is **not**
  garbage-collected. Delete that directory to reclaim the space.
- An inaccurate ASR transcript for the speaker reference cannot be overridden
  without a manual `clone_voice`: `DubRenderRequest` has no `speaker_ref_text`
  field in this release.
- `models download --no-wait` is still accepted but ignored (L1, unchanged).

## [0.4.0] — 2026-11-26 — `9d6ede1`

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

[Unreleased]: https://github.com/000jd/agent-voice/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/000jd/agent-voice/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/000jd/agent-voice/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/000jd/agent-voice/compare/v0.3.3...v0.4.0
[0.3.3]: https://github.com/000jd/agent-voice/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/000jd/agent-voice/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/000jd/agent-voice/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/000jd/agent-voice/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/000jd/agent-voice/commit/f93f4dd
[0.2.0]: https://github.com/000jd/agent-voice/commit/00b8b46
[0.1.0]: https://github.com/000jd/agent-voice/commit/5f5224b
