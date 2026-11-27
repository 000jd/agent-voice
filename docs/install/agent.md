# Connect an agent

Install first — one command, or from source — then come back here:

```bash
curl -fsSL https://raw.githubusercontent.com/000jd/agent-voice/main/scripts/install.sh | sh
```

Both routes and their flags: [one-command.md](one-command.md). The installer puts the
shim at `~/.local/bin/agent-voice` and runs `agent-voice setup`, which ends by printing
the snippet this page documents.

`agent-voice mcp` is a **stdio shim**. It starts the daemon if it is not already
running (under `data/daemon.lock`, so two agents cannot race), then proxies MCP to
the daemon's Streamable HTTP endpoint. The agent process never loads a model, and
there is exactly one GPU owner. The shim spawns the daemon with `cwd=<home>` and
`AGENT_VOICE_HOME` set, so the client's working directory cannot change which `.env`
the daemon reads.

Every configuration below runs the same command with the absolute path to your home
substituted for `<abs-path>`. That is the checkout root for a source install, or the
installer's directory (`~/.local/share/agent-voice` by default) — `agent-voice
mcp-config` prints the resolved value for you:

```bash
uv run --directory <abs-path> agent-voice mcp
```

## Generate a config with `mcp-config`

Instead of hand-editing paths:

```bash
uv run agent-voice mcp-config --client claude-code
```

`mcp-config` prints a ready-to-paste JSON snippet for the named client, with your
resolved home's absolute path and the matching `AGENT_VOICE_CLIENT_ID` already filled
in. The home is `AGENT_VOICE_HOME` when set, otherwise the checkout/install directory,
otherwise `~/.agent-voice` — so the snippet is right even when you are not standing in
the project. `--client` is required; the output goes to stdout, nothing is written to
disk, and no daemon is started.

| `--client` value | Emits |
|---|---|
| `claude-code` | A `.mcp.json`-shaped `mcpServers` block for Claude Code |
| `claude-desktop` | A `claude_desktop_config.json`-shaped `mcpServers` block |
| `vscode` | A `.vscode/mcp.json`-shaped `servers` block (with `type`) |
| `cursor` | A Cursor `mcp.json`-shaped `mcpServers` block |
| `command-code` | A `.mcp.json` block with `AGENT_VOICE_CLIENT_ID=command-code` |

Redirect it, or copy the printed JSON:

```bash
uv run agent-voice mcp-config --client claude-desktop > /tmp/agent-voice-claude.json
```

The snippets below are what that output looks like; they are safe to copy by hand
if you prefer. `mcp-config` omits `"type": "stdio"` for the four `mcpServers`
clients because stdio is their default; VS Code requires it explicitly.

> `AGENT_VOICE_CLIENT_ID` is sent by the shim as the `X-Agent-Id` header. It
> selects a per-agent voice/engine binding (`set_agent_binding`). It is
> convenience routing on a local single-user box, **not** an authorization
> boundary.

> If your client launches with a minimal environment (GUI apps usually do), put
> the **absolute** path to `uv` in `"command"` — e.g. `/home/you/.local/bin/uv`
> instead of `uv` — and make sure `ffmpeg`/`ffprobe` are on the `PATH` that
> process inherits. That is the single most common "server failed to start" cause.

> Add `"AGENT_VOICE_HOME": "<abs-path>"` to the `env` block when the daemon should use
> a home other than the one `mcp-config` detected (a second checkout, a sandbox, a test
> data directory). The shim forwards it and spawns the daemon with it, so the client's
> own working directory never matters.

## Claude Code

Project scope — `.mcp.json` at the repository root:

```json
{
  "mcpServers": {
    "agent-voice": {
      "command": "uv",
      "args": ["run", "--directory", "<abs-path>", "agent-voice", "mcp"],
      "env": {
        "AGENT_VOICE_CLIENT_ID": "claude-code"
      }
    }
  }
}
```

The same `mcpServers` shape works as a user-scope config for any client that reads
one.

## Claude Desktop

Config file locations:

- Linux: `~/.config/Claude/claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "agent-voice": {
      "command": "uv",
      "args": ["run", "--directory", "<abs-path>", "agent-voice", "mcp"],
      "env": {
        "AGENT_VOICE_CLIENT_ID": "claude-desktop"
      }
    }
  }
}
```

Restart Claude Desktop after editing. On Windows, use the `uv.exe` path and a
Windows-style `<abs-path>`, e.g. `C:\\Users\\you\\agent-voice`.

## VS Code

Put a top-level `servers` object in `.vscode/mcp.json` (workspace) or your
profile's `mcp.json`. Note the key is **`servers`**, not `mcpServers`, and `type`
is required:

```json
{
  "servers": {
    "agent-voice": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "<abs-path>", "agent-voice", "mcp"],
      "env": {
        "AGENT_VOICE_CLIENT_ID": "vscode"
      }
    }
  }
}
```

## Cursor

`~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project):

```json
{
  "mcpServers": {
    "agent-voice": {
      "command": "uv",
      "args": ["run", "--directory", "<abs-path>", "agent-voice", "mcp"],
      "env": {
        "AGENT_VOICE_CLIENT_ID": "cursor"
      }
    }
  }
}
```

## Command Code

`.mcp.json` in the project (or your user-scope equivalent):

```json
{
  "mcpServers": {
    "agent-voice": {
      "command": "uv",
      "args": ["run", "--directory", "<abs-path>", "agent-voice", "mcp"],
      "env": {
        "AGENT_VOICE_CLIENT_ID": "command-code"
      }
    }
  }
}
```

## HTTP MCP directly

When the daemon is already running, a Streamable HTTP client can mount the
endpoint without a proxy:

```
http://127.0.0.1:8000/mcp/
```

The trailing slash matters (`/mcp` redirects with `307`). Send
`X-Agent-Id: <name>` to select a binding. If `AGENT_VOICE_API_KEY` is set, send
`Authorization: Bearer <key>` — the mounted `/mcp` app is covered by the same
guard as everything else.

```json
{
  "mcpServers": {
    "agent-voice": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp/"
    }
  }
}
```

Key names differ per client (VS Code uses `servers`); the URL and headers are the
same everywhere.

## LangChain

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient({
    "agent-voice": {"transport": "http", "url": "http://127.0.0.1:8000/mcp/"}
})
tools = await client.get_tools()
```

## OpenAI-compatible clients

Anything that speaks the OpenAI audio API can use `POST /v1/audio/speech` directly
— `input`, `voice`, `response_format`, `speed`, and `model` (`tts-1`, `tts-1-hd`,
or `engine:model`). Builtin OpenAI voice names are mapped to local voices or
preset speakers; the response carries `x-agent-voice-speaker` when a preset
answered. Full rules: [../api.md](../api.md#post-v1audiospeech-openai-compatible).

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="not-needed-unless-set")
client.audio.speech.create(model="tts-1", voice="nova", input="Hello.").write_to_file("out.wav")
```

## CPU-only install (Kokoro + Phonon)

An agent host without a GPU can still **speak and listen**: Kokoro-82M produces
speech and Phonon-2.5 transcribes, both on the CPU with **0 MB of VRAM** — on
`device=cpu`, which is the verified pair for each.

The fastest path is the wizard, which detects the CPU-only host and installs exactly
these two:

```bash
uv run agent-voice setup --profile cpu-only        # or: --non-interactive -y
```

By hand:

```bash
# speak
uv run agent-voice engines install kokoro
uv run agent-voice models download kokoro kokoro-82m   # 363 MB, once
uv run agent-voice engines test kokoro                 # 7/7 IPC v1 conformance

# listen
uv run agent-voice engines install phonon
uv run agent-voice models download phonon phonon-2.5   # 156 MB, once
uv run agent-voice engines test phonon                 # 7/7 on the ASR table

uv run agent-voice doctor                              # espeak-ng + asr rows + weights
AGENT_VOICE_DEFAULT_ENGINE=kokoro AGENT_VOICE_DEFAULT_ASR_ENGINE=phonon \
  uv run agent-voice serve
```

As soon as the Kokoro engine ships in `engines/`, `doctor` grows an `espeak-ng`
row: `PASS` when the binary is on `PATH`, `WARN` with the platform install command
when it is not — common English still speaks, so it never fails the run. Install it
with `sudo apt install espeak-ng` (Debian/Ubuntu) or `brew install espeak-ng`
(macOS).

It also gains an `asr` row — the configured ASR engine plus its model aliases, or a
`WARN` naming `agent-voice engines install phonon` — and a `dub filters` row for
`sidechaincompress`/`alimiter`. All of them are WARNs: a TTS-only host and an ffmpeg
with a plain mixer are both supported setups.

The eight preset voices — `af_heart`, `af_bella`, `af_sarah`, `am_adam`,
`am_michael`, `bf_emma`, `bm_george`, `bm_lewis` — need no reference clip, so
`clone_voice` is optional on this host:

```python
speak("Nightly build finished.", speaker="af_heart")
```

An OpenAI-shaped client keeps working unchanged: its six builtin names map to
Kokoro presets (`alloy` → `af_heart`, `nova` → `af_bella`, …), and every response
carries `x-agent-voice-speaker`.

### `inbox_dir` covers transcription input too

`server_info().inbox_dir` is not only where a reference clip has to live. It is the
confinement root for **every** path an agent supplies:

- `clone_voice(ref_audio_path=…)` — a 3–10 s reference clip;
- `transcribe(audio_path=…)` — any audio or video file to read;
- `dub_prepare(media_path=…)` — the video to re-voice.

So an operator who wants an agent to summarise a folder of recordings needs to put
those recordings (or a staging copy) under `<DATA_DIR>/inbox/`, or set
`MCP_BASE_PATH` to the shared directory. `AGENT_VOICE_ALLOW_ANY_PATH=true` is the
broad escape hatch and should be reserved for a trusted local agent. Without one of
those, the agent gets `403 PATH_OUTSIDE_BASE` before any ffmpeg work — nothing is
read, and the fix is to move the file, not to retry.

The CLI `agent-voice transcribe <path>` deliberately uses the same resolver, so a
command that works at a prompt also works for an agent.

## Autostart

### Linux (systemd user unit)

```ini
# ~/.config/systemd/user/agent-voice.service
[Unit]
Description=agent-voice daemon
After=default.target

[Service]
Type=simple
WorkingDirectory=%h/projects/agent-voice
Environment=AGENT_VOICE_HOME=%h/projects/agent-voice
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=%h/projects/agent-voice/.venv/bin/agent-voice serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now agent-voice
systemctl --user status agent-voice
journalctl --user -u agent-voice -f
```

The `Environment=PATH=…` line is required, not decoration. A systemd user unit
does not inherit your login shell's `PATH`, and `ffmpeg`/`ffprobe` and `uv` must
resolve **from the daemon's own environment**: ffmpeg canonicalises every
reference clip and converts output formats, and `uv` is what installs engine
venvs when `engines install` runs through the API. Without it you get
`ffmpeg not found on PATH` while your interactive shell sees ffmpeg fine.
`%h/.local/bin` is where a `uv` installed with the standard installer lives;
adjust the paths if yours differ, and confirm with:

```bash
systemctl --user show-environment | tr ' ' '\n' | grep '^PATH='
```

`Environment=AGENT_VOICE_HOME=…` is what makes the unit read the config you edited:
the daemon reads `<home>/.env`, and a unit's `WorkingDirectory` **no longer decides**
which `.env` is used. Keeping both lines is still good practice (relative paths are
easier to reason about that way), but the `AGENT_VOICE_HOME` line is the one that
matters. Confirm what the daemon resolved with `agent-voice doctor` — its `home` row
prints `home=`, `data=`, `engines=` and `config=`.

### Windows (NSSM)

```powershell
nssm install agent-voice "C:\path\to\agent-voice\.venv\Scripts\agent-voice.exe" serve
nssm set agent-voice AppDirectory "C:\path\to\agent-voice"
nssm set agent-voice AppEnvironmentExtra "AGENT_VOICE_HOME=C:\path\to\agent-voice"
nssm start agent-voice
```

Alternatively, Task Scheduler with trigger "At log on" and action
`agent-voice.exe serve` in the project directory; set `AGENT_VOICE_HOME` in the task's
environment rather than relying on "Start in", which no longer selects the `.env`.
Windows support (including `scripts/install.ps1`) is **experimental and unverified** —
see [../troubleshooting.md](../troubleshooting.md#platform-notes) if it fails.

## Verify the connection

```bash
curl -s localhost:8000/health                    # {"status":"ok", ...}
uv run agent-voice status                        # daemon health + GPU state
uv run agent-voice doctor                        # full environment checklist
```

`doctor` is the fastest triage when an agent reports "server failed to start": it
prints one row per check — including `espeak-ng` once the Kokoro engine ships and
`ffmpeg`/`ffprobe` always — and a `fix:` line under every `WARN`/`FAIL`.

Then ask the agent to call **`server_info`** first. It is free, never spawns a
worker, and tells the agent the version, default engine, registered voices,
`inbox_dir`, preset speakers and GPU state in one call. An agent that starts
there does not need to guess.
