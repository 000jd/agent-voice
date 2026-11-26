# Security policy

## Supported versions

`agent-voice` is pre-1.0. Security fixes land on the latest release line; older
lines are supported on a best-effort basis.

| Version | Supported | Notes |
|---|---|---|
| `0.3.x` | **Yes** | Current line; v0.3.3 contains the WebSocket guard and inbox confinement |
| `0.2.x` | Best effort | Fixes are considered, but new releases target `0.3.x` |
| `0.1.x` | No | v0.1 shipped unsafe defaults (mutation on, unauthenticated `/audio`, no Host/Origin guard); upgrade |

## Reporting a vulnerability

**Do not open a public issue.**

Use GitHub's private reporting flow: on the repository's **Security** tab, choose
**Report a vulnerability** (GitHub Security Advisories). That gives us a private
thread and lets a fix and an advisory be prepared together.

Please include:

- The version (`agent-voice status`, `GET /health`, or `server_info().version`)
  and, when relevant, the commit hash.
- Platform, GPU and driver (`nvidia-smi`), and the engine involved.
- The configuration that matters: whether `AGENT_VOICE_API_KEY` is set, the bind
  address, `ALLOW_AGENT_ENGINE_MUTATION`, `AGENT_VOICE_ALLOW_ANY_PATH`,
  `MCP_BASE_PATH`.
- A minimal reproduction (request body, CLI command, client config) and the
  impact you believe it has.
- Any suggested fix.

What to expect: acknowledgement within a few days, an assessment of severity and
scope, and a fix released with an advisory that credits you unless you ask
otherwise. This is a small pre-1.0 project, so timelines are best effort rather
than contractual. Please give us a reasonable window before publishing details.

## Threat model

### The assumed deployment

`agent-voice` is a **local, single-user** service. It runs on the same machine as
the person who installed it, binds a loopback address, and is driven by an AI
agent over MCP or HTTP. Nothing leaves the machine: no telemetry, no analytics,
no cloud calls except the explicit Hugging Face weight downloads you request.

### In scope

| Threat | Mitigation |
|---|---|
| A web page in the user's browser reaching the daemon by DNS rebinding or a cross-origin fetch | Pure ASGI Host/Origin guard on **HTTP and WebSocket**: foreign `Host` → `421`, foreign `Origin` → `403`, applied to `/v1`, `/audio`, `/mcp` and the socket alike |
| Another local user or process on a shared box speaking/cloning through an open port | Loopback bind by default; a non-loopback bind without `AGENT_VOICE_API_KEY` refuses to start; bearer required for `/v1`, `/audio` and `/mcp`; WebSocket rejects until a token is presented (close `1008`, before `accept()`) |
| Reading another user's synthesized speech or voice clone through the artifact endpoint | `/audio/{artifact_id}` is behind the same bearer as `/v1`; ids are strict 16 lowercase hex and the v0.1 wildcard glob is gone, so enumeration is not possible |
| Path traversal / tricking the daemon into reading arbitrary files | Agent-supplied `clone_voice` paths are confined to `<DATA_DIR>/inbox/` (or `MCP_BASE_PATH`) unless the operator sets `AGENT_VOICE_ALLOW_ANY_PATH=true`; escape is `PATH_OUTSIDE_BASE` before any ffmpeg probe |
| An agent escalating itself into writing and running code | `ALLOW_AGENT_ENGINE_MUTATION=false` by default; when enabled it is loopback-only, audited to `data/mutations.log` with a `worker.py` hash, and `REQUIRE_ENGINE_APPROVAL=true` stages plugins under `engines/.pending/<id>/` until a human runs `engines approve` |
| An agent's tool surface being larger than necessary | `AGENT_VOICE_TOOLSET=core` hides all engine/model management; `auto` exposes it only when mutation is enabled |
| Runaway requests consuming the GPU or unbounded memory | Input caps enforced at admission; FIFO queue bounded by `MAX_QUEUE_DEPTH` → `429 BACKPRESSURE`; streaming backpressure cancels a stalled consumer |

### Out of scope

- A hostile user with the **same OS account**. They can read `data/`, the `.env`
  with the API key, and the engine venvs directly. File permissions on the data
  directory are your responsibility.
- A compromised operating system, driver, Python interpreter, or `uv`/PyPI supply
  chain.
- Malicious model weights. Weights are downloaded from Hugging Face into the
  standard cache and executed by the engine locally; treat a model repository
  like any other third-party dependency.
- Denial of service by someone who can already bind your loopback interface or
  exhaust your disk.
- Impersonation enabled by **consent you failed to obtain** (see below).

## Security defaults, explicitly

Change these only with intent, and prefer `.env` over exporting them ad hoc:

| Setting | Default | What it means |
|---|---|---|
| `AGENT_VOICE_HOST` | `127.0.0.1` | Not reachable off-box |
| `AGENT_VOICE_API_KEY` | empty | Convenient locally; **set it** before you expose anything, and note a non-loopback bind refuses to start without one |
| `AGENT_VOICE_ALLOWED_HOSTS` | `[]` | Loopback names only, plus anything you add deliberately |
| `ALLOW_AGENT_ENGINE_MUTATION` | `false` | Otherwise an agent writes Python the daemon executes |
| `REQUIRE_ENGINE_APPROVAL` | `true` | Human gate on agent-authored plugins |
| `AGENT_VOICE_ALLOW_ANY_PATH` | `false` | Agent reference reads confined to the inbox |
| `AGENT_VOICE_TOOLSET` | `auto` | `core` for an agent that only needs to make audio |

If you front the daemon with a reverse proxy, you are outside the supported
topology: the Host/Origin guard is designed for loopback, and a proxy must pass
the correct headers, terminate auth itself, and be added to
`AGENT_VOICE_ALLOWED_HOSTS`. Do not expose the port directly to the internet.

## Voice cloning: consent is the operator's responsibility

Only clone voices you have permission to use.

`agent-voice` runs locally and offline — no audio, transcript or reference clip
leaves the machine — but that is a property of the software, not a licence.
Cloning a real person's voice without their consent is impersonation, and in many
jurisdictions a legal problem, no matter where the model runs. The tool cannot
verify consent for you; the responsibility is the operator's.

Practical expectations:

- Get explicit permission from the speaker, and keep a record of it.
- Consider how the output will be labelled. Synthesized speech should not be
  presented as a recording of the real person.
- Delete voice references you no longer need (`agent-voice voices delete
  <voice_id>`); the reference audio lives under `data/voices/`.
- Generating audio of a real person to deceive, defraud or harass is prohibited
  by the project's intent regardless of the licence.

## Agent-specific guidance

- Start tools in `core` (`AGENT_VOICE_TOOLSET=core`) and widen only if the agent
  genuinely needs model/engine management.
- Treat `X-Agent-Id` as routing, never as authentication. It is a header any
  caller can set; it selects a binding, it does not grant access.
- Give each agent its own binding (`set_agent_binding`) rather than sharing a
  global default voice.
- Read `server_info()` first: it is free, side-effect-free, and tells the agent
  what it can actually do instead of provoking failed requests.
- The `core` and `full` MCP surfaces exist so a model never sees a tool it is
  configured not to use. Prefer removing a capability to relying on a model to
  avoid it.
