# One-command install

The installers fetch a **release tag**, put the project on disk, write an
`agent-voice` shim, and hand over to `agent-voice setup`. They are the fastest
supported path; the from-source install below is the one CI and contributors use and
stays fully supported.

```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/000jd/agent-voice/main/scripts/install.sh | sh
```

```powershell
# Windows (PowerShell) — EXPERIMENTAL, unverified on real Windows
irm https://raw.githubusercontent.com/000jd/agent-voice/main/scripts/install.ps1 | iex
```

Audit before you run it — `--dry-run` prints every action and every `argv` and writes
nothing at all:

```bash
curl -fsSL https://raw.githubusercontent.com/000jd/agent-voice/main/scripts/install.sh \
  | sh -s -- --dry-run
```

> Read the script first if you prefer: it is plain POSIX `sh` at
> [`scripts/install.sh`](../../scripts/install.sh), `set -eu`, no sudo, no telemetry.
> The trust decision behind `curl | sh` is documented in
> [SECURITY.md](../../SECURITY.md#supply-chain-and-the-one-command-installer).

## What it does, in order

1. **Resolves the version.** The default is the **latest release tag**, found with
   `git ls-remote --tags --refs` and semver-sorted. `main` is installed only when you
   ask for it explicitly with `--ref main`. A `--version 0.6.0` pins a tag; `--ref`
   pins any branch or commit.
2. **Checks the prerequisites.** `git` is required. `uv` is installed with the
   official installer **only if it is missing**, and only after you say yes — `--yes`
   answers that prompt for you. Nothing is installed with `sudo`.
3. **Clones or updates** into `~/.local/share/agent-voice` (or `--dir <path>`), and
   runs `uv sync` for the core project. The core venv installs no torch.
4. **Creates the shim** `~/.local/bin/agent-voice`, which sets `AGENT_VOICE_HOME` to
   the install directory and execs `<install>/.venv/bin/agent-voice`. If
   `~/.local/bin` is not on your `PATH`, it says so and prints the line to add.
5. **Writes `<install>/.env`** with absolute paths: `AGENT_VOICE_HOME`, `DATA_DIR` and
   `ENGINES_DIR`. An existing `.env` is backed up first.
6. **Runs `agent-voice setup`** unless `--no-setup` was passed. An interactive terminal
   gets the wizard; a non-interactive run gets the non-interactive defaults.

On Windows the mirror uses `%LOCALAPPDATA%\agent-voice` for the install,
`%LOCALAPPDATA%\agent-voice-data` for the data root, and writes an
`<install>\bin\agent-voice.cmd` shim.

## Your data lives outside the install directory

The default data root is a **sibling** of the install directory:

```text
~/.local/share/agent-voice/          install (code, venvs, .env)
~/.local/share/agent-voice-data/     data   (voices, lexicons, cache, jobs, artifacts)
~/.cache/huggingface/                model weights (the standard HF cache)
```

That is deliberate: `<install>/.env` records **absolute** `DATA_DIR` and `ENGINES_DIR`
paths, so re-installing, moving the install directory or `--uninstall` never costs you
registered voices, pronunciation lexicons, the synthesis cache or downloaded weights.

`--uninstall` removes the shim and the install directory and **never** the data
directory; it prints the data path it kept so you can delete it yourself if you mean to.

## Flags

`install.sh` (POSIX `sh`) — pass them after `sh -s --`:

| Flag | Effect |
|---|---|
| `--dir <path>` | Install directory. Default `~/.local/share/agent-voice` |
| `--version <tag>` | Install a specific release tag (`v0.6.0`; a bare `0.6.0` is accepted) |
| `--ref <ref>` | Install a branch, tag or commit — `main` only when named |
| `--repo <path-or-url>` | Install from another clone or remote. A local path is cloned directly and needs no network for the source |
| `--yes` | Accept prompts (including "install uv?"). Never downloads model weights by itself |
| `--profile auto\|cpu-only\|nvidia-gpu\|apple-silicon` | Passed to `agent-voice setup` |
| `--download` / `--no-download` | Passed to `agent-voice setup`: fetch weights now or later |
| `--no-setup` | Install only; run `agent-voice setup` yourself later |
| `--uninstall` | Remove the shim and install directory; never the data directory |
| `--dry-run` | Print every action and `argv`; write nothing, download nothing |
| `--help` | Usage |

`install.ps1` mirrors them: `-Dir`, `-Version`, `-Ref`, `-Repo`, `-Yes`, `-Profile`,
`-Download`, `-NoDownload`, `-NoSetup`, `-Uninstall`, `-DryRun`. **The PowerShell
installer is experimental and unverified on real Windows** — it is parse-checked when
`pwsh` is available, and nothing more is claimed for it.

## After the installer: `agent-voice setup`

`setup` is the same wizard the installer runs, and it is safe to re-run at any time.
It probes the host (zero torch: `nvidia-smi`, `sysctl`/`/proc/meminfo`, `rocm-smi`/
`rocminfo`, `shutil.which`), recommends **only verified** `(engine, device)` pairs, and
applies the plan.

```bash
agent-voice setup                            # interactive, on a real TTY
agent-voice setup --dry-run                  # show the plan, change nothing
agent-voice setup -y --profile nvidia-gpu    # non-interactive defaults
agent-voice setup --non-interactive --device cpu --engines kokoro,phonon
agent-voice setup -y --download              # also fetch the recommended weights
```

| Flag | Effect |
|---|---|
| `-y`, `--yes` | Apply without asking. Required when there is no terminal. **Never downloads weights on its own** |
| `--non-interactive` | Ask nothing. Also implied when stdin is not a TTY |
| `--profile auto\|cpu-only\|nvidia-gpu\|apple-silicon` | Choose the recommendation family instead of detecting it |
| `--device auto\|cpu\|cuda\|mps\|rocm` | Global device preference, written as `AGENT_VOICE_DEVICE` |
| `--engines kokoro,phonon` | Install exactly these engines |
| `--download` / `--no-download` | Fetch model weights now (the prompt shows the total GB) or leave them for later |
| `--dry-run` | Print hardware, recommendations, download sizes and the `.env` diff. Installs nothing, downloads nothing, writes nothing |

Interactive vs not: the wizard is interactive **only when stdin is a real TTY** (and not
for `--yes`, `--non-interactive` or `--dry-run`). The interactive questions are device
preference → which engines (pre-checked with the verified defaults) → download weights
now? (with the total GB shown) → a final confirmation before anything is applied.

With no terminal and no `--yes`, `setup` prints the plan, applies **nothing**, and exits
non-zero telling you to re-run with `--yes`. That is deliberate: writing `.env` and
installing venvs should not happen because a pipe closed.

**`--yes` alone never downloads weights.** Only an explicit `--download`, or saying yes
to the interactive weights prompt, starts a download.

### What it writes

- **`.env`, atomically.** The existing file is read, comments and unknown keys are
  preserved byte for byte, only known keys are updated, and new keys are appended under
  the right banner. The write is atomic (`os.replace`), mode `0600`, and the API key is
  never echoed.
- **A backup first.** `.env` → `.env.bak`; if `.env.bak` already exists, the backup is
  `.env.<UTC>.bak`. An existing backup is never overwritten.
- **Absolute paths.** `DATA_DIR`, `ENGINES_DIR` and `AGENT_VOICE_HOME` are absolute in
  the file, so the daemon resolves them the same way from any working directory.
- **Device settings.** `AGENT_VOICE_DEVICE` and, per engine, `AGENT_VOICE_DEVICES`.
- **The defaults you chose.** `AGENT_VOICE_DEFAULT_ENGINE` / `AGENT_VOICE_DEFAULT_ASR_ENGINE`.

### What it prints

1. Detected hardware as one line (`os/arch`, cores, RAM, GPU and VRAM or "CPU only").
2. The recommended engines with their device and level.
3. Download sizes, when you asked for a dry run and when you are about to download.
4. The `.env` diff, before it is applied.
5. `doctor` afterwards — **optional** rows are `INFO`, so a healthy CPU box reads as a
   few `PASS`, a couple of `INFO`, and no failures.
6. A ready-to-paste `mcp-config` snippet, plus the per-`(engine, device)` table with
   `verified` / `experimental` labels.

### `doctor` severity means something

| Status | Meaning | Rank | Exits non-zero? |
|---|---|---|---|
| `PASS` | Required and present | 0 | no |
| `INFO` | Optional and absent, and you did not select it — an uninstalled engine you are not using, weights you have not downloaded, no accelerator on a CPU box | 1 | no |
| `WARN` | Optional and absent, but selected or expected — the default engine's venv, a missing `.env`, a missing `espeak-ng` | 2 | no |
| `FAIL` | Required and broken | 3 | **yes** |

The summary reads `N checks, X optional, Y warning(s), no failures`; only a `FAIL`
changes the exit code. That is why a fresh clone, a CPU-only laptop and a GPU box with
three unused engines are all green-ish rather than alarming. `doctor` also prints the
resolved `home`, `data`, `engines` and `config` paths and each installed engine's
device.

## Where the config lives: the home

`AGENT_VOICE_HOME` is the one anchor. In order:

1. `AGENT_VOICE_HOME`, when set.
2. The first parent of the installed package that contains **both** `pyproject.toml` and
   `engines/` — i.e. the checkout (or the installer's install directory).
3. `~/.agent-voice`, reported by `doctor` as a row rather than created silently.

Relative `DATA_DIR` and `ENGINES_DIR` values resolve against that home, and the daemon
reads `<home>/.env`. Two consequences worth knowing:

- **It works from any directory.** Running `agent-voice engines list` in `/tmp` uses the
  checkout's `data/` and `engines/`, and creates nothing in `/tmp`.
- **Autostart no longer depends on `WorkingDirectory`.** A systemd unit's
  `WorkingDirectory=` does not decide which `.env` is read; `AGENT_VOICE_HOME` (or the
  install location) does. The MCP stdio shim also passes `cwd=<home>` and
  `AGENT_VOICE_HOME` to the daemon it spawns, so an MCP client's cwd cannot move the
  config.

The prefixed aliases `AGENT_VOICE_DATA_DIR`, `AGENT_VOICE_ENGINES_DIR` and
`AGENT_VOICE_MCP_BASE_PATH` are accepted alongside `DATA_DIR`, `ENGINES_DIR` and
`MCP_BASE_PATH`; the `AGENT_VOICE_*` spelling wins when both are set. See
[../config.md](../config.md#paths).

## Devices

A device is part of an engine's dependency set, not a runtime flag — the committed
`uv.lock` bakes the wheel index into every torch entry, so CPU, CUDA and ROCm are
different lock roots.

```bash
agent-voice engines install qwen3_tts --device cpu              # verified
agent-voice engines install kokoro --device cuda --experimental # experimental
agent-voice engines install <id> --device <d> --dry-run         # which project would it sync?
```

- `--device auto|cpu|cuda|mps|rocm` selects the variant project under
  `engines/<id>/variants/<device>/` (or the canonical project when it already matches),
  syncs it with `uv sync --project … --locked` and `UV_PROJECT_ENVIRONMENT` pointed at
  the canonical `engines/<id>/.venv`.
- `--experimental` is required for an `experimental` pair and never unlocks an
  `unsupported` one. It grants permission, not a lock: a pair also needs a shipped variant
  project, and the refusal lists the variants this checkout actually has.
  `qwen3_tts_fast` is CUDA-only; `phonon` has no CUDA wheel at all.
- Switching the device of an installed engine **replaces that engine's venv**. Only one
  variant per engine is resident at a time, and the manager's torch probe turns a stale
  variant into a clear `409 ENGINE_NOT_INSTALLED` naming the reinstall command.
- The **0 MB VRAM** claim holds for `device=cpu` only. `kokoro` and `phonon` on cuda/mps
  report real memory and are `experimental`.

The proven index URLs and the variant layout are in
[../engines.md](../engines.md#device-variants); the settings are in
[../config.md](../config.md#hardware).

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/000jd/agent-voice/main/scripts/install.sh \
  | sh -s -- --uninstall
```

or, from a clone, `scripts/install.sh --uninstall`. It removes the shim and the install
directory, prints the data directory it kept, and touches nothing else. Delete the data
directory yourself if that is what you want — it holds your voices and lexicons.

## From source instead

```bash
git clone https://github.com/000jd/agent-voice.git agent-voice
cd agent-voice
uv sync --extra dev && cp .env.example .env
uv run agent-voice doctor
uv run agent-voice setup          # optional: same wizard, same .env writer
```

A checkout is its own home (rule 2 above), so `DATA_DIR=./data` in a copied `.env`
resolves to `<checkout>/data` no matter where you run the command from. Keep the
`.env` the wizard writes — it uses absolute paths.

## If the installer stopped

Interruptions are safe and re-runnable: nothing outside the install directory and the
shim is written before the `.env`, and the `.env` write is atomic. Re-run the same
command; it updates the clone instead of cloning again. See
[../troubleshooting.md](../troubleshooting.md#the-installer-stopped-halfway).
