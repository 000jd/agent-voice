# Contributing to agent-voice

Thanks for helping. This project has one hard invariant and a short list of house
rules; everything else is ordinary review.

## Set up

Requirements: Linux (the reference platform) or Windows, Python 3.12, `uv`, and
`ffmpeg`/`ffprobe` on `PATH`.

```bash
git clone https://github.com/000jd/agent-voice.git agent-voice
cd agent-voice
uv sync --extra dev && cp .env.example .env && uv run agent-voice doctor
```

`uv sync --extra dev` is the supported dev install — a bare `uv sync` installs
neither ruff nor pytest, because they live in the `dev` optional-dependency
group. The core venv must never contain torch.

### Layout

| Path | What lives there |
|---|---|
| `src/agent_voice/` | Core daemon: FastAPI app, routes, MCP server, CLI, cache, voice/engine registry, worker manager |
| `src/agent_voice/ipc/` | The frozen IPC v1 protocol and transport |
| `engines/<id>/` | Engine plugins (`engine.yaml`, `worker.py`, optional `pyproject.toml`) |
| `tests/` | Mock-backed and contract tests; GPU tests are marked and skipped by default |
| `docs/` | Reference documentation (the README is a landing page) |
| `skills/agent-voice/SKILL.md` | Agent-facing playbook, also served as the `agent-voice://guide` resource |
| `examples/` | Runnable end-to-end examples |

## The one hard rule: the core daemon never imports torch

`src/agent_voice/` holds no model code, and `pyproject.toml`'s core
`dependencies` must never gain torch. That is what makes "0 MB VRAM when idle" a
structural guarantee instead of a promise. Consequences:

- Engine dependencies go in `engines/<id>/pyproject.toml`, installed into that
  engine's own `uv` venv.
- Typer sub-commands must not import torch; defer heavy imports into the command
  body.
- The two Qwen backends need mutually exclusive `transformers` versions. Never
  try to merge their dependency sets.

## Lint and tests

```bash
uv run ruff check .                    # must stay clean
uv run pytest -v                       # mock + contract suite; no GPU, no torch
.venv/bin/pytest -v                    # same, via the console script
uv run pytest -v -m "not gpu"          # explicit; addopts already excludes gpu
```

`[tool.pytest.ini_options]` sets `pythonpath = ["."]`, so the repo root is
importable for the console script too; no `PYTHONPATH` workaround is needed.

Guidelines for tests:

- Cover behaviour through the public surface (REST, MCP, CLI, IPC) rather than
  private helpers. The suite runs entirely against the `mock` engine.
- Never require a GPU, network access or a secret in the default suite. Anything
  that does belongs behind the `gpu` marker.
- Keep the suite fast. It currently runs in about a minute; a change that makes
  teardown wait on a timeout is a bug.
- A new error path needs a test that asserts the `{error_code, message,
  suggestion}` body, not just the status code.

### Mock engine conformance

`agent-voice engines test <engine_id>` spawns a real worker and walks the whole
IPC v1 contract, printing a pass/fail row per step and exiting non-zero on any
failure. It is the fastest end-to-end check and needs no GPU:

```bash
uv run agent-voice engines test mock
```

Run it after touching anything in `worker.py`, the transport or the worker
manager. A change to the IPC wire format is out of scope: the protocol is frozen
at v1 and `mode`/`speaker`/`instruct` are optional fields on `synthesize`.

### GPU tests (opt-in)

The `gpu` marker is skipped by default. The real gate needs a CUDA GPU and
downloaded weights:

```bash
AGENT_VOICE_RUN_GPU_TESTS=1 uv run pytest -v -m gpu -o addopts=""
```

`-o addopts=""` is required: the default `addopts` excludes `gpu` before `-m` is
applied. CI sets `AGENT_VOICE_RUN_GPU_TESTS=0` explicitly and runs no GPU tests.

## Code style

- `ruff check .` with `line-length = 100`, rules `E,F,I,UP,B` (B008 ignored for
  Typer). Formatting follows the same rules; match the surrounding code.
- Full type hints on public functions; docstrings explain *why*, not *what*.
- Errors are `AgentVoiceError(ErrorCode.…, message, suggestion)` from
  `schemas.py`. Add a code to the enum and its `HTTP_STATUS_BY_ERROR` entry, never
  inline a new string at a call site.
- Every subprocess is an argv list with `shell=False`. Do not add a path that
  builds a shell string.
- New admission caps belong at admission — before routing, before a worker is
  spawned, before any GPU time — and need a `.env.example` entry plus a
  documented default.
- New security-sensitive defaults ship **safe**: mutation off, paths confined,
  loopback only. Read [SECURITY.md](SECURITY.md) before changing a guard.
- Heavy imports (torch, engine backends, manim) stay out of module import time.

## Documentation

- `README.md` is a landing page (roughly 250 lines): hero, install, quickstarts,
  architecture, security summary, docs index. Reference detail belongs in `docs/`.
- Put API/endpoint detail in `docs/api.md`, configuration in `docs/config.md`,
  engine and GPU behaviour in `docs/engines.md`, failures in
  `docs/troubleshooting.md`, and client installation in `docs/install/agent.md`.
- Keep every command copy-pasteable and shell-safe. Prefer tables over prose for
  fields, defaults and error codes.
- Every docs link must resolve. If you move a section, update its inbound links
  (`grep` for the old anchor).
- Add an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for any
  user-visible change — Added / Changed / Fixed / Security, Keep a Changelog
  style.
- `docs/phase0-report.md` is a point-in-time hardware measurement record. Do not
  retro-edit its numbers; add a dated section or new measurements to
  `docs/engines.md` instead.

## Pull requests

1. Branch from `main`; keep the change focused.
2. `uv run ruff check .` and `uv run python -m pytest -v` must be green.
3. `uv run agent-voice engines test mock` must pass if you touched anything in
   the worker/transport/manager path.
4. Add the changelog entry, and documentation for anything a user must know.
5. Say what you could not verify — platform, GPU model, driver — in the PR body.
   Honest "untested on Windows" is better than an unverified claim.

## Releasing (maintainers)

1. Confirm the gates above on the release commit, plus the GPU suite on a CUDA
   box.
2. Bump the version in `pyproject.toml` **and** `src/agent_voice/__init__.py`
   (they must match; `server_info` and `/health` report the latter).
3. Move the `## [Unreleased]` section of `CHANGELOG.md` to
   `## [x.y.z] — YYYY-MM-DD` and add the compare link.
4. Tag `vX.Y.Z` and push. Do not rewrite history on a tag that has been
   published.

## Security issues

Do not open a public issue for a vulnerability. Follow [SECURITY.md](SECURITY.md).
