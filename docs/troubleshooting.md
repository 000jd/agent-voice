# Troubleshooting

Start with the daemon's own checks — they are cheaper than guessing.

```bash
uv run agent-voice doctor     # environment, ffmpeg, .env, engine venvs, models, daemon health
uv run agent-voice status     # daemon health, torch_loaded, GPU/queue state
uv run agent-voice engines test mock   # IPC v1 conformance without a GPU (~1 s)
```

`doctor` prints one `PASS`/`FAIL`/`WARN` row per check — Python, `uv`, `ffmpeg`,
the GPU (via `nvidia-smi`), the bind port (free, or already serving a healthy
daemon), one row per engine, cached weights, the inbox and `.env` — with a `fix:`
line under every problem. It exits non-zero only on a `FAIL`: a CPU-only box, an
uninstalled GPU engine or a missing `.env` are warnings, not failures.

For a GPU engine, `agent-voice engines test qwen3_tts_fast` walks the same
protocol end to end and isolates "the worker cannot start / cannot load" from
"the request was wrong".

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

## Common errors and what they mean

| Symptom | Cause | Fix |
|---|---|---|
| `401 unauthorized` | `AGENT_VOICE_API_KEY` is set and the request had no/wrong bearer token | Send `Authorization: Bearer <key>`; on a WebSocket use `Sec-WebSocket-Protocol: bearer, <key>` |
| WebSocket closes with `1008` | Host, Origin or token failed the guard | Connect via `127.0.0.1`/`localhost`; send the token; add a deliberate hostname to `AGENT_VOICE_ALLOWED_HOSTS` |
| `421 host not allowed` | The `Host` header is not loopback or in `AGENT_VOICE_ALLOWED_HOSTS` | Connect by loopback name, or add the name you front it with |
| `403 origin not allowed` | A browser page on another origin called the daemon | Serve the page from loopback, or add its host to `AGENT_VOICE_ALLOWED_HOSTS` |
| `404 VOICE_NOT_FOUND` for an artifact | Artifacts are ephemeral and the id is unknown/expired | Re-run the synthesis; ids are 16 lowercase hex chars |
| `400 BAD_REQUEST` for an artifact id | The id contained path or glob syntax | Use the id exactly as returned |
| `422 VOICE_REQUIRED` | No voice exists or was selected, and the engine cannot speak without one | Clone a voice (`voices add`), use `speaker=`, or `design_voice` |
| `403 PATH_OUTSIDE_BASE` | An agent-supplied `ref_audio_path` escaped `MCP_BASE_PATH` or the inbox | Put the clip in `<DATA_DIR>/inbox/` (`server_info().inbox_dir`) or set `AGENT_VOICE_ALLOW_ANY_PATH=true` deliberately |
| `403 AGENT_MUTATION_DISABLED` | Engine/model mutation is off, or the caller is not on loopback | Set `ALLOW_AGENT_ENGINE_MUTATION=true` **and** call from loopback, or make the change as the operator |
| `409 WORKER_BUSY` | `unload` without `force` while a job runs, an engine switch while the active engine drains, or a minimum-residency window | Retry after the job, or `agent-voice unload --force` |
| `409 ENGINE_NOT_INSTALLED` | The engine is discovered but its venv is missing | `agent-voice engines install <id>` |
| `422 MANIFEST_INVALID` | `engine.yaml` failed validation | Fix the manifest; the message names the field |
| `429 BACKPRESSURE` | The per-engine wait queue (`MAX_QUEUE_DEPTH`) is full, or a stream consumer stalled | Retry, raise `MAX_QUEUE_DEPTH` deliberately, or submit long work with `start_script_job` |
| `413 TEXT_TOO_LONG` | `MAX_TEXT_CHARS`, `MAX_INSTRUCT_CHARS`, `MAX_DESIGN_SAMPLE_CHARS` or `MAX_REF_TEXT_CHARS` exceeded | Shorten the input or raise the cap |
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
```

## Cache looks stale or too large

```bash
uv run agent-voice cache stats   # entries, size, cap, oldest/newest
uv run agent-voice cache clear   # deletes cached artifacts and sidecars, prints bytes freed
```

The cache key is a SHA-256 over the full synthesis parameter set, including the
spoken (lexicon-rewritten) text, model alias, mode, speaker, instruct, seed, take
and backend identity. Editing a pronunciation invalidates only the lines that use
that term. If you suspect a stale entry, `cache clear` is safe — it only removes
renders, never voices or pronunciation data.

## Streaming stalls

- `BACKPRESSURE` with "client stopped reading" means the consumer paused longer
  than `STREAM_QUEUE_TIMEOUT_SECONDS` (default 5 s). Read the response body
  promptly or request shorter text.
- `native` vs `emulated` streaming is reported per response
  (`x-agent-voice-streaming-mode`, or `streaming_mode` on the WebSocket
  `started` frame). `qwen3_tts` is emulated by design.
- Barge-in (sending a new `speak` over the WebSocket while audio flows) cancels
  the in-flight stream first; a `cancel` message is acknowledged with
  `{"type":"cancelled"}`.

## Platform notes

- **macOS.** Only the `mock` engine runs. There is no CUDA, so `qwen3_tts` and
  `qwen3_tts_fast` cannot load; `doctor` flags the missing GPU as a `WARN`, which
  is expected there. Use `AGENT_VOICE_DEFAULT_ENGINE=mock` for development.
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
