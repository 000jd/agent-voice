# Connect an agent

`agent-voice mcp` is a **stdio shim**. It starts the daemon if it is not already
running (under `data/daemon.lock`, so two agents cannot race), then proxies MCP to
the daemon's Streamable HTTP endpoint. The agent process never loads a model, and
there is exactly one GPU owner.

Every configuration below runs the same command with the absolute path to your
checkout substituted for `<abs-path>`:

```bash
uv run --directory <abs-path> agent-voice mcp
```

## Generate a config with `mcp-config`

Instead of hand-editing paths:

```bash
uv run agent-voice mcp-config --client claude-code
```

`mcp-config` prints a ready-to-paste JSON snippet for the named client, with this
checkout's absolute path and the matching `AGENT_VOICE_CLIENT_ID` already filled
in. `--client` is required; the output goes to stdout, nothing is written to disk
and nothing is started.

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

`WorkingDirectory` also determines which `.env` is read, so point it at the
checkout.

### Windows (NSSM)

```powershell
nssm install agent-voice "C:\path\to\agent-voice\.venv\Scripts\agent-voice.exe" serve
nssm set agent-voice AppDirectory "C:\path\to\agent-voice"
nssm start agent-voice
```

Alternatively, Task Scheduler with trigger "At log on" and action
`agent-voice.exe serve` in the project directory (set "Start in" to the project
path so `.env` is found). Windows support is untested — see
[../troubleshooting.md](../troubleshooting.md#platform-notes) if it fails.

## Verify the connection

```bash
curl -s localhost:8000/health                    # {"status":"ok", ...}
uv run agent-voice status                        # daemon health + GPU state
uv run agent-voice doctor                        # full environment checklist
```

Then ask the agent to call **`server_info`** first. It is free, never spawns a
worker, and tells the agent the version, default engine, registered voices,
`inbox_dir`, preset speakers and GPU state in one call. An agent that starts
there does not need to guess.
