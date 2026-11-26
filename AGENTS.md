# AGENTS.md

`agent-voice` is a headless, UI-free voice-cloning and speech-synthesis server that an AI agent
drives over MCP or HTTP. The agent asks for speech, gets back a file path or a PCM stream, and
the GPU stays at 0 MB the rest of the time.

## Architecture, in six bullets

- **The core daemon never imports torch.** `src/agent_voice/` (FastAPI + MCP + CLI) holds no
  model code; that is a structural guarantee of the idle-GPU claim, not a convention.
- **Isolated per-engine venvs.** Each engine is a directory under `engines/<engine_id>/` with an
  `engine.yaml` manifest and a `worker.py`, installed into its own `uv` venv. Two engines that
  need mutually exclusive dependency sets coexist without conflict.
- **Five checkpoints, three modes.** `engine.yaml` declares five Qwen3-TTS 12Hz aliases — two
  `clone` (`1.7b-base`, `0.6b-base`), two `custom_voice` (`1.7b-custom`, `0.6b-custom`, nine
  `preset_speakers`, `supports_instruct` on the 1.7B only) and one `voice_design`
  (`1.7b-design`). Routing picks a model by `mode`, so a `speaker=` request never lands on a
  checkpoint that can only clone.
- **IPC v1 is a frozen contract.** Length-prefixed, multiplexed frames over a Unix domain socket
  (loopback TCP on Windows). Correlation ids let `cancel` and `health` land while a decode is
  running. See `src/agent_voice/ipc/protocol.py` and `src/agent_voice/ipc/transport.py` — do not
  change the wire format. `mode`, `speaker` and `instruct` are **optional** fields on
  `synthesize`/`stream_pcm`; omitting them is exactly v1 behaviour, and a worker that does not
  understand them may ignore them.
- **20 MCP tools** (12 core), gated into a `core` and a `full` surface (`AGENT_VOICE_TOOLSET`).
  The mutation tools that write and execute Python are hidden unless explicitly enabled.
- **`skills/agent-voice/SKILL.md` is the playbook.** It is the agent-facing guide, and the
  `/mcp` `guide` resource serves it. Change agent-facing behaviour there, not in prose scattered
  through the README.

## Commands

```bash
# tests (mock engine only; needs no GPU and no torch)
.venv/bin/pytest -v                       # or: uv run pytest -v
.venv/bin/pytest -v -m "not gpu"          # explicit; same thing (addopts already excludes gpu)

# for the pytest console script, make the repo root importable:
PYTHONPATH=. .venv/bin/pytest -v          # conftest does `from tests._fake_clock import ...`

# real GPU gate (opt-in, needs a CUDA GPU + downloaded weights)
AGENT_VOICE_RUN_GPU_TESTS=1 uv run pytest -v -m gpu -o addopts=""

# lint — must stay clean
.venv/bin/ruff check .

# end-to-end IPC v1 conformance against a live worker (~1 s, no GPU)
.venv/bin/agent-voice engines test mock

# design a voice from a description, in-process, no daemon needed
.venv/bin/agent-voice voices design narrator \
  --instruct "warm baritone, measured pace, late-night radio"

# the same path on the mock engine, which ships custom_voice and voice_design aliases
.venv/bin/agent-voice voices design probe --engine mock --model mock-design \
  --instruct "quiet and precise"
```

`engines test <id>` is the fastest way to prove an engine still speaks protocol v1: it spawns a
real worker and walks `spawn+connect → hello → load_model → synthesize → cancel → health → shutdown`,
printing a pass/fail row per step and exiting non-zero on any failure. Use it after touching
anything in the worker, the transport or the manager.

`voices design` is the same in-process pattern the conformance test and the Manim demo use: build
an `AppContext`, `await ctx.workers.start()`, call the service, `await ctx.workers.stop()` inside
`asyncio.run`. No running daemon is involved, which is why every heavy import stays inside the
command body.

## Security defaults

- **Engine mutation is off by default.** `ALLOW_AGENT_ENGINE_MUTATION=false`. Enabling it lets an
  AI agent write `engine.yaml` / `worker.py` / `pyproject.toml` into `engines/` *and execute it*,
  because `worker.py` is run as a Python script. That is arbitrary code execution requested by a
  language model; leave it off unless you specifically want that.
- **Loopback only.** Even with mutation enabled, both the daemon and the calling client must be on
  loopback. A non-loopback peer gets `AGENT_MUTATION_DISABLED`.
- **Loopback by default end to end.** The server binds `127.0.0.1`, a Host/Origin guard rejects
  DNS-rebinding attempts, and `/audio` sits behind the bearer token because an artifact is the
  user's own speech. Set `AGENT_VOICE_API_KEY` before exposing anything.
- `REQUIRE_ENGINE_APPROVAL=true` stages agent-authored plugins under `engines/.pending/<id>/`
  for `agent-voice engines approve <id>` instead of activating them.

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
- Typer sub-commands must not import torch. Defer heavy imports into command bodies.
- `pyproject.toml`'s core `dependencies` must never gain torch.
