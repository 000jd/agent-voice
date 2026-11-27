# Troubleshooting

Start with the daemon's own checks — they are cheaper than guessing.

```bash
uv run agent-voice doctor     # environment, ffmpeg, .env, engine venvs, models, daemon health
uv run agent-voice status     # daemon health, torch_loaded, GPU/queue state
uv run agent-voice engines test mock   # IPC v1 conformance without a GPU (~1 s)
```

`doctor` prints one `PASS`/`FAIL`/`WARN` row per check — Python, `uv`, `ffmpeg`,
the GPU (via `nvidia-smi`), `espeak-ng` (when the Kokoro engine ships), the bind
port (free, or already serving a healthy daemon), one row per engine, cached
weights, the inbox and `.env` — with a `fix:` line under every problem. It exits
non-zero only on a `FAIL`: a CPU-only box, an uninstalled GPU engine, a missing
`espeak-ng` or a missing `.env` are warnings, not failures.

For a GPU engine, `agent-voice engines test qwen3_tts_fast` walks the same
protocol end to end and isolates "the worker cannot start / cannot load" from
"the request was wrong". For Kokoro, `agent-voice engines test kokoro` proves the
CPU engine in about a second of decode after the weights are downloaded.

## CUDA graph capture fails (`qwen3_tts_fast`)

**Symptoms.** The worker dies during `load_model`; `engines test qwen3_tts_fast`
fails at graph capture; or the first request returns a CUDA/torch traceback
instead of a clean `{error_code, message, suggestion}` body.

Graph capture needs a clean device. Another process holding VRAM, a driver that
disallows capture, or a card that cannot replay the captured graphs will fail it.

**Two ways out:**

1. **Fall back to the official engine.** Set
   `AGENT_VOICE_DEFAULT_ENGINE=qwen3_tts` (or pass `engine="qwen3_tts"` per call),
   install it with `agent-voice engines install qwen3_tts`, and download the same
   aliases. Slower per request, streaming emulated — but reliable.
2. **Retry with VRAM free.** Stop whatever else is using the GPU, confirm with
   `nvidia-smi`, evict a previous worker with `agent-voice unload --force` (or
   `unload_models(force=true)`), then restart the daemon so capture happens with
   the device to itself.

If neither works, the engine is not the right fit for that box; `qwen3_tts` is
supported and documented precisely for this case.

## `qwen3_tts_fast` cannot load the tokenizer

**Symptoms.** Loading a model fails with:

```text
AttributeError: property 'upsample' of 'Qwen3TTSTokenizerV2Encoder' has no setter
```

`faster-qwen3-tts` requires `transformers==5.15.1`, pinned exactly in
`engines/qwen3_tts_fast/pyproject.toml`. Newer releases (5.24.x and later)
define `upsample` as a read-only property that the model's `__init__` tries to
assign.

**Fix.** Re-apply the pin rather than upgrading inside that venv:

```bash
uv run agent-voice engines install qwen3_tts_fast   # uv sync restores the pinned set
```

Do not bump it by hand. `qwen3_tts_fast` and the official `qwen3_tts` need
mutually exclusive `transformers` versions, which is why they are separate venvs
in the first place. The same mistake on `qwen3_tts` looks like an import error
from `qwen_tts`; reinstall that engine the same way.

## `ffmpeg` / `ffprobe` missing

Both binaries are required, and the daemon needs them on **its own** `PATH` — a
shell that has ffmpeg while a systemd unit or MCP shim does not is the usual
failure.

**Symptoms.**

- `clone_voice` / `design_voice` fails with `ffmpeg not found on PATH` (or
  `ffprobe`), because every reference is canonicalised — decoded, resampled to
  24 kHz mono WAV, silence-trimmed — before it is registered.
- A non-WAV `format=` (`mp3`, `opus`, `flac`, `pcm`) fails during conversion,
  because every output format goes through ffmpeg.
- `engines test` passes but `voices add` fails: the worker has ffmpeg, the daemon
  does not.

**Fix.** Install the system package (`sudo apt install ffmpeg`,
`brew install ffmpeg`, or a Windows build) and confirm both resolve from the
environment the daemon starts in:

```bash
ffmpeg -version && ffprobe -version
```

For a systemd user unit, add the binaries' directory to the unit's `PATH`
explicitly — see the fixed unit in
[install/agent.md](install/agent.md#linux-systemd-user-unit). For an MCP stdio
server, the `env` block in the client config is what the shim inherits.

## `espeak-ng` is missing (Kokoro)

**Symptoms.** `agent-voice doctor` prints a `WARN` row:

```text
espeak-ng   WARN   not on PATH (Kokoro out-of-dictionary and non-English G2P)
                   fix: sudo apt install espeak-ng (or your distribution's package)
```

Kokoro's English G2P is spaCy-based and handles common words; `espeak-ng` is the
**out-of-dictionary fallback** (names, invented words) and the backend for every
non-English voice. So a missing binary is a warning, not a failure: ordinary
English still speaks.

**Fix.**

```bash
sudo apt install espeak-ng     # Debian/Ubuntu
brew install espeak-ng         # macOS
espeak-ng --version            # confirm the daemon's PATH sees it
```

A systemd unit or MCP client needs the binary on **its own** `PATH`, the same way
ffmpeg does.

## `Can't find model 'en_core_web_sm'` (Kokoro)

**Symptoms.** The Kokoro worker dies on its first request, or `engines test kokoro`
fails inside `load_model`, with:

```text
OSError: [E050] Can't find model 'en_core_web_sm'. It doesn't seem to be a Python
package or a valid path to a data directory.
```

`misaki` (Kokoro's G2P) needs spaCy's small English model. Its own installer picks
the **active** interpreter — in a spike it wrote into the repo's `.venv`, not the
engine venv — so the model can be installed somewhere the worker never looks.

**Fix.** Reinstall the engine; `engines/kokoro/pyproject.toml` pins
`en-core-web-sm` as a direct URL dependency, so `uv sync` puts it in the right venv:

```bash
uv run agent-voice engines install kokoro
```

Then check the engine venv itself, not your shell:

```bash
engines/kokoro/.venv/bin/python -c "import spacy; spacy.load('en_core_web_sm')"
```

## Kokoro pulled a CUDA wheel (VRAM is not 0)

**Symptoms.** `nvidia-smi` shows memory held by a Kokoro worker, or
`engines/kokoro/.venv/bin/python -c "import torch; print(torch.version.cuda)"`
prints a version instead of `None`, or `uv pip list` shows `nvidia-*` packages.

**Cause.** `torch` resolved from PyPI instead of the CPU index. Installing
`kokoro` alone makes torch a *transitive* dependency, and the PyPI wheel is the
CUDA build — it drags in every `nvidia-*` package and lets `KPipeline` auto-select
the GPU.

**Fix.** Reinstall from the pinned project so the explicit CPU source applies:

```bash
uv run agent-voice engines install kokoro
engines/kokoro/.venv/bin/python -c "import torch; print(torch.__version__, torch.version.cuda)"
# expected: 2.x.y+cpu None
```

Do not `pip install torch` inside that venv by hand. The engine's
`pyproject.toml` declares `[[tool.uv.index]] name = "pytorch-cpu"` and
`[tool.uv.sources] torch = { index = "pytorch-cpu" }`; the exact lock is recorded in
[phase0-kokoro-report.md](phase0-kokoro-report.md).

## Phonon install fails (or is slow on the first call)

**Symptoms.** `agent-voice engines install phonon` fails, or `engines test phonon`
times out at `load_model`, or the first `transcribe` takes ~50 s.

**Causes, in order of likelihood.**

1. **A CUDA torch came in.** `fermion-research` needs `torch`, and installing it alone
   resolves the CUDA wheel from PyPI (every `nvidia-*` package plus Triton, ~2 GB).
   `engine install phonon` uses the pinned `pyproject.toml`, so this should not happen —
   but if you installed by hand, check:

   ```bash
   engines/phonon/.venv/bin/python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
   # expected: 2.x.y+cpu False
   ```

   Fix by reinstalling through the project, never by hand-patching the venv.

2. **The first `load_speech` is legitimately slow.** Measured on a Ryzen 7 5700X:
   **50.1 s** for the first call (0.16 GB download, SHA-256 verification of every
   packed member, then unpack to `~/.cache/fermion/speech/FermionResearch__Phonon-2.5/`),
   and **1.05 s** on every later load. `load_model` has a 600 s budget for exactly this.
   If a `transcribe` looks hung for under a minute on first use, it is not hung.

3. **No wheel for this platform, or no network.** Pre-download the weights and install
   from a cache. The error text from `uv` names the missing wheel or the failed URL —
   read it rather than retrying; a genuinely unsupported platform will not start
   working. `docs/phase0-asr-report.md` records the exact recipe that was verified.

4. **`FERMION_CACHE_DIR` points somewhere unwritable.** The registry checks that
   directory *and* `~/.cache/fermion` when deciding whether a model is downloaded, so a
   redirected-but-empty cache reads as "not downloaded". Make it writable, or unset it.

**Verify:**

```bash
uv run agent-voice models download phonon phonon-2.5
uv run agent-voice engines test phonon      # 7/7 on the ASR table
nvidia-smi --query-compute-apps=pid,used_memory --format=csv   # empty, before and after
```

## `sidechaincompress` / `alimiter` missing (dubbing degrades)

**Symptoms.** `agent-voice doctor` prints a `dub filters` row:

```
dub filters   WARN   missing: sidechaincompress
              fix: dubbing with keep_original_bed=true will fall back to a plain mix
```

A dub still renders. The original audio bed is mixed with `amix` + gain instead of
being ducked under the voice, so the source is audible throughout instead of
stepping back when somebody speaks.

**Cause.** A minimal or old ffmpeg build. These are standard filters in every modern
build (Debian/Ubuntu `ffmpeg` since 4.x, Homebrew, the static builds from
ffmpeg.org), but a trimmed distro package, a container image, or an ancient
`ffmpeg` can lack them.

**Check by hand:**

```bash
ffmpeg -hide_banner -filters | grep -E 'sidechaincompress|alimiter'
```

**Fix.**

- Install a fuller ffmpeg: `sudo apt install ffmpeg` on Debian/Ubuntu,
  `brew install ffmpeg` on macOS, or a static build from
  <https://ffmpeg.org/download.html>. Make sure the binary the **daemon** sees is the
  one you fixed — a systemd unit inherits its own `PATH`.
- Or work around it: `dub render ... --no-bed` replaces the original audio entirely,
  which needs no ducking filter and always works.

Do not treat this as a FAIL: a dub that renders with a plain mix is correct, just less
polished, and `doctor` exits 0 for it.

## `PATH_OUTSIDE_BASE` on a transcription

**Symptoms.**

```json
{
  "error_code": "PATH_OUTSIDE_BASE",
  "message": "path escapes the inbox (/repo/data/inbox): /home/me/videos/talk.mp4",
  "suggestion": "Put the file in /repo/data/inbox (the inbox), or set AGENT_VOICE_ALLOW_ANY_PATH=true to allow any existing local path."
}
```

**Cause.** Transcription reads files through exactly the same resolver as
`clone_voice` (`resolve_media_path` → `resolve_ref_audio_path`), so a path outside the
confinement root is refused. That is deliberate: transcription would otherwise be a
hole through the inbox boundary that cloning carefully enforces. The refusal happens
**before** any ffmpeg work, so nothing was read.

The base is, in order: `MCP_BASE_PATH` when set; otherwise `<DATA_DIR>/inbox/`;
otherwise (with `AGENT_VOICE_ALLOW_ANY_PATH=true`) any existing absolute path.

**Fix**, in order of least surprise:

1. **Move the file into the inbox** — `mv /home/me/videos/talk.mp4 data/inbox/` — and
   re-run with `data/inbox/talk.mp4`. No configuration change, and it is what
   `server_info().inbox_dir` is telling an agent to do.
2. **`AGENT_VOICE_ALLOW_ANY_PATH=true`** for a trusted local agent that genuinely needs
   arbitrary reads. Restart the daemon afterwards.
3. **`MCP_BASE_PATH=/home/me/videos`** when the goal is a shared working directory. This
   also relocates output artifacts to `<MCP_BASE_PATH>/artifacts`.
4. **A symlink is not a workaround.** It is refused in every branch — resolved real
   paths, including symlinked ancestors, are what get compared.

The CLI `agent-voice transcribe <path>` goes through the same resolver on purpose: a
command that works at a prompt works for an agent, and vice versa. If the CLI refuses
your path, so would the agent.

## Markup is spoken aloud, or a line is rejected

**Symptoms.** The audio says "bracket pause four hundred milliseconds" instead of
pausing, or a request fails with `400 BAD_REQUEST` mentioning `[pause]`.

- **A wanted tag was not parsed.** Only `[pause]`, `[slow]`, `[fast]`, `[emphasis]`
  and `[spell]` exist, and paired tags need `[/slow]`-style closers. Tag names are
  case-insensitive (`[Pause 500ms]` parses). `[pausex]`, `[div]` and `[[x]]` are
  literal text and are spoken as written; a whitelisted tag inside literal brackets
  still parses, so `[[pause]]` is a literal `[]` plus a pause (use `markup=false`
  when the brackets themselves are the subject).
- **A tag value is malformed.** `[pause abc]`, `[pause -1s]` and `[pause 99s]` are
  a recognised tag with an impossible value: that is a `400 BAD_REQUEST` naming the
  accepted forms, because silently narrating it would misrepresent the audio.
- **The text is legitimate markup syntax.** Pass `markup=false` to speak brackets
  literally — `arr[0]`, `matrix[i][j]` and `[1, 2]` are literal even with markup on.
- **Too many tags.** 128 tags, nesting depth 4 and 32 segments per utterance are
  structural caps; a longer line must be split into beats.
- **Subtitles look wrong.** Captions come from `display_text` (tags stripped,
  `[spell]` words kept as written). If a caption shows a tag, the tag was not
  recognised — check the spelling against the table above.

## Common errors and what they mean

| Symptom | Cause | Fix |
|---|---|---|
| `401 unauthorized` | `AGENT_VOICE_API_KEY` is set and the request had no/wrong bearer token | Send `Authorization: Bearer <key>`; on a WebSocket use `Sec-WebSocket-Protocol: bearer, <key>` |
| WebSocket handshake fails with `403` | Host, Origin or token failed the guard. The refusal happens **before** `accept()`, so a real client sees a failed HTTP upgrade, not a close code | Connect via `127.0.0.1`/`localhost`; send the token; add a deliberate hostname to `AGENT_VOICE_ALLOWED_HOSTS` |
| `421 host not allowed` | The `Host` header is not loopback or in `AGENT_VOICE_ALLOWED_HOSTS` | Connect by loopback name, or add the name you front it with |
| `403 origin not allowed` | A browser page on another origin called the daemon | Serve the page from loopback, or add its host to `AGENT_VOICE_ALLOWED_HOSTS` |
| `404 VOICE_NOT_FOUND` for an artifact | Artifacts are ephemeral and the id is unknown/expired | Re-run the synthesis; ids are 16 lowercase hex chars |
| `400 BAD_REQUEST` for an artifact id | The id contained path or glob syntax | Use the id exactly as returned |
| `400 BAD_REQUEST` mentioning `[pause]` | A recognised markup tag with a value it cannot honour (`[pause abc]`, `[pause 99s]`) | Use `[pause]`, `[pause 500ms]` or `[pause 1.5s]` (0–10 s), or `markup=false` |
| Markup tags are spoken aloud | The tag is not in the whitelist (`[pausex]`, `[div]`) or `markup=false` was sent | Check the spelling; tag names are case-insensitive; see [api.md](api.md#expressive-markup) |
| `422 VOICE_REQUIRED` | No voice exists or was selected, and the engine cannot speak without one | Clone a voice (`voices add`), use `speaker=`, or `design_voice`. The suggestion now names the inbox, e.g. `clone_voice(name='narrator', ref_audio_path='/repo/data/inbox/clip.wav', …)` — copy that path rather than inventing one |
| `403 PATH_OUTSIDE_BASE` | A caller-supplied `ref_audio_path`, `transcription` `audio_path` or dub `media_path` escaped its confinement base — normally `<DATA_DIR>/inbox/`, or `MCP_BASE_PATH` when set — or was reached through a symlink | Move the file into `server_info().inbox_dir` and pass that path. If the shared directory is genuinely elsewhere, the operator can set `MCP_BASE_PATH` or `AGENT_VOICE_ALLOW_ANY_PATH=true`. See [the transcription variant below](#path_outside_base-on-a-transcription) |
| `422 INVALID_REF_AUDIO` | The clip exists but could not be decoded as usable audio: corrupt, an unsupported container, or shorter than the 1 s minimum for a reference | Re-export it (`ffmpeg -i in -ac 1 -ar 24000 out.wav`) and use 3–10 s of clean speech. For `transcribe`, an unreadable file is a `BAD_REQUEST` carrying the ffprobe reason instead |
| `403 AGENT_MUTATION_DISABLED` | Engine/model mutation is off, or the caller is not on loopback | Set `ALLOW_AGENT_ENGINE_MUTATION=true` **and** call from loopback, or make the change as the operator |
| `409 WORKER_BUSY` | `unload` without `force` while a job runs, an engine switch while the active engine drains, or a minimum-residency window | Retry after the job, or `agent-voice unload --force` |
| `409 ENGINE_NOT_INSTALLED` | The engine is discovered but its venv is missing — including an ASR engine you asked for by name | `agent-voice engines install <id>`; for transcription that is usually `agent-voice engines install phonon` |
| `422 MANIFEST_INVALID` | `engine.yaml` failed validation | Fix the manifest; the message names the field |
| `429 BACKPRESSURE` | The global wait queue (`MAX_QUEUE_DEPTH`) is full, or a stream consumer stalled | Retry, raise `MAX_QUEUE_DEPTH` deliberately, or submit long work with `start_script_job` |
| `413 TEXT_TOO_LONG` | `MAX_TEXT_CHARS`, `MAX_INSTRUCT_CHARS`, `MAX_DESIGN_SAMPLE_CHARS` or `MAX_REF_TEXT_CHARS` exceeded | Shorten the input or raise the cap |
| `400 BAD_REQUEST` naming a media duration | The clip is longer than `MAX_TRANSCRIBE_SECONDS` (default 3600) | Split it, or raise `MAX_TRANSCRIBE_SECONDS` deliberately |
| `422 BAD_REQUEST` naming 25 hotwords | `hotwords` exceeded `MAX_HOTWORDS` — a structural cap, not an env knob | Trim the list. Whitespace-only entries are already dropped for you |
| `400 BAD_REQUEST` on a voice name | Name does not match `^[\w .\-]{1,64}$` | Use letters, digits, spaces, dots, hyphens, 1–64 chars |
| `410 DAEMON_RESTARTED` | The job was pending/running when the daemon stopped; jobs cannot resume | Re-submit the script |
| `503 CUDA_OOM` | The engine caught a real CUDA out-of-memory | Use a smaller alias (`0.6b-*`), free VRAM, or `unload(force=true)` first |
| `500 INTERNAL_ERROR` | Unhandled daemon error | Read the daemon log (below) for the traceback |

Every error body is `{error_code, message, suggestion}`; the suggestion is meant
to be actionable, so read it before changing configuration.

## Daemon not reachable

```bash
uv run agent-voice status
```

answers `daemon not reachable at http://127.0.0.1:8000` when nothing is
listening. Check, in order:

1. The daemon is running — `uv run agent-voice serve` in another shell, or
   `systemctl --user status agent-voice`.
2. Nothing else owns the port: `ss -ltnp | grep 8000`.
3. `AGENT_VOICE_HOST` / `AGENT_VOICE_PORT` match where you are connecting.
4. `AGENT_VOICE_API_KEY` is set: `/health` stays open, but `agent-voice status`
   calls `/v1/gpu` without a token, so the GPU line prints the `401` error body
   instead of a status. That is the guard working, not a broken daemon.

If a second daemon was started by an MCP client, do not start a third: the stdio
shim takes `data/daemon.lock` precisely so one daemon owns the GPU.

## Model reported as not downloaded although the directory exists

A bare `refs/main` pointer is not a model. `models list` reports
`downloaded_locally` only when real weight files (`.safetensors`, `.bin`, `.pt`,
`.gguf`) exist with nonzero size. Re-run the download:

```bash
uv run agent-voice models download qwen3_tts_fast 1.7b-base
uv run agent-voice models download kokoro kokoro-82m
```

`models download --no-wait` is accepted but ignored: the CLI always blocks until the
snapshot is complete, because a background download would leave the caller with no
job id to poll. Use the REST route (`POST /v1/models/{engine}/{alias}/download`) or
`download_model()` when you want an asynchronous job.

## Cache looks stale or too large

```bash
uv run agent-voice cache stats   # entries, size, cap, oldest/newest
uv run agent-voice cache clear   # deletes cached artifacts and sidecars, prints bytes freed
```

The cache key is a SHA-256 over the full synthesis parameter set, including the
spoken text after the lexicon, the effective speed, model alias, mode, speaker,
instruct, seed, take and backend identity. v0.4.0 added **no key fields**: `markup`,
`spell_identifiers`, `paragraph_gap_ms` and `project` are not part of the key, but
their effect on the spoken text and on `speed` is. So:

- editing a pronunciation, or changing a tag that changes the spoken words or
  speed, invalidates only the lines that use it;
- a pause or paragraph gap is local silence and is never cached;
- `[emphasis]` gain is applied at stitch time, after the per-segment cache entry;
- `pronunciation_hash` is observability only (`pronunciations show`).

If you suspect a stale entry, `cache clear` is safe — it removes renders, never
voices or lexicon data.

## Streaming stalls

- `BACKPRESSURE` with "client stopped reading" means the consumer paused longer
  than `STREAM_QUEUE_TIMEOUT_SECONDS` (default 5 s). Read the response body
  promptly or request shorter text.
- `native` vs `emulated` streaming is reported per response
  (`x-agent-voice-streaming-mode`, or `streaming_mode` on the WebSocket
  `started` frame) — `streaming_mode` is present **only** on a streamed response.
  `qwen3_tts` is emulated by design; `qwen3_tts_fast` and `kokoro` are native.
- Barge-in (sending a new `speak` over the WebSocket while audio flows) cancels
  the in-flight stream first; a `cancel` message is acknowledged with
  `{"type":"cancelled"}`.
- A bad frame does not close the socket: malformed JSON and recoverable errors
  (`TEXT_TOO_LONG`, an unknown speaker) answer with an `error` frame and the
  connection stays usable.

## Platform notes

- **macOS.** The CPU engines run: `kokoro` and `mock`. There is no CUDA, so
  `qwen3_tts` and `qwen3_tts_fast` cannot load; `doctor` flags the missing GPU as a
  `WARN`, which is expected there. Use `AGENT_VOICE_DEFAULT_ENGINE=kokoro` (or
  `mock`) for development.
- **Windows.** Untested. The IPC transport switches to loopback TCP
  automatically. Engine venvs use the Windows layout (`Scripts/` rather than
  `bin/`) while the shipped manifests declare a POSIX `venv_python` path — that
  mismatch is one of the things nobody has verified on Windows yet. If something
  fails, run `agent-voice doctor`, then `engines test mock`, and report the output
  with the bug template.
- **Linux.** The reference platform. UDS IPC plus CUDA is the tested path.

## Where the logs are

| Source | Location |
|---|---|
| Daemon started by the MCP shim | `<DATA_DIR>/daemon.log` |
| Daemon started in a shell | That terminal (uvicorn log) |
| Daemon started by systemd | `journalctl --user -u agent-voice -f` |
| Mutation audit | `<DATA_DIR>/mutations.log` — one JSON line per engine/model mutation |
| Job history | `<DATA_DIR>/jobs/` |

`GET /health` reports `torch_loaded`. It must be `false` in the core daemon — if
it is `true`, the core venv has torch in it and the idle-GPU guarantee is gone.
