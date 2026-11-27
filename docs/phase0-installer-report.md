# Phase 0 — One-command installer safety and verification

This report records the contract, safety rules and tests for `scripts/install.sh`
and `scripts/install.ps1`. It was assembled from the implemented scripts and their
local runs; the original research agent failed before writing a separate report, so
the evidence below is the actual validation performed on this host.

## Host and scope

| | |
|---|---|
| Host | Pop!_OS 22.04, x86-64, RTX 4060 Ti 16 GB |
| Shells | `dash` (POSIX `sh`) and `bash` |
| PowerShell | **absent** — `scripts/install.ps1` was not parsed or executed here |
| Network installs | deliberately **not** run (no `uv` bootstrap, no `git clone` from GitHub, no `setup`) |
| Local install test | `--repo <local checkout>` with a fake `uv` and a temp `HOME` |

## Flag contract

Both scripts expose the same surface; PowerShell uses the `-` spelling.

| Flag | Default | Behaviour |
|---|---|---|
| `--dir <path>` | `~/.local/share/agent-voice` (`%LOCALAPPDATA%\agent-voice`) | Install directory (checkout + root venv) |
| `--version <tag>` | latest release tag | Exact tag; a bare `0.6.0` normalises to `v0.6.0` |
| `--ref <branch|sha>` | — | Opt-in edge; mutually exclusive with `--version` |
| `--repo <url|path>` | GitHub repo | A local path is provably offline |
| `--yes` | false | Consent to install `uv` and to non-interactive setup |
| `--profile <name>` | `auto` | Forwarded to `agent-voice setup` |
| `--download` / `--no-download` | setup decides | Forwarded; `--never` implies no download |
| `--no-setup` | false | Install only, do not run the wizard |
| `--uninstall` | false | Remove shim + install dir, never the data dir |
| `--dry-run` | false | Print every action/argv and write nothing |
| `--help` | — | Complete usage, exit 0 |

## Non-negotiable safety rules

1. **Tag-pinned by default.** `--version` is the latest release tag resolved with
   `git ls-remote --tags --refs`, filtered to `vX.Y.Z` and numeric-sorted. `main`
   is used only with an explicit `--ref main`. An empty tag list is a hard error.
2. **No silent sudo.** The scripts never call `sudo`. `uv` is installed through
   the official user-level installer only when missing, after consent unless
   `--yes`.
3. **No shell strings.** Every subprocess is an argv invocation. The only pipe is
   the documented `curl … | sh` / `irm … | iex` entrypoint itself.
4. **Data outside the install dir.** `DATA_DIR` defaults to a sibling
   (`…/agent-voice-data`) and is written **absolute** in `<install>/.env`, together
   with absolute `ENGINES_DIR` and `AGENT_VOICE_HOME`. `--uninstall` removes the
   install dir and shim, never the data dir, and prints its path.
5. **Refuse a dirty checkout.** An existing install dir must be a git repository
   with no modified **tracked** files. Untracked installer artifacts (`.env`,
   `.venv`, `.env.bak`) do not block an update.
6. **Never overwrite a backup.** `.env` is copied to `.env.bak`; if `.env.bak`
   already exists, `.env.<UTC>.bak` (with a numeric suffix if needed) is used.
7. **`--dry-run` is inert by construction.** It prints the clone/sync/env/shim
   plan and performs no filesystem writes, no `uv` install and no network fetch.
8. **Install-dir guardrails.** `--uninstall` refuses `/` and `$HOME`.
9. **Shim.** POSIX writes `~/.local/bin/agent-voice` with `AGENT_VOICE_HOME` and
   `exec <install>/.venv/bin/agent-voice "$@"`; PowerShell writes
   `<install>\bin\agent-voice.cmd` calling `.venv\Scripts\agent-voice.exe`. A
   missing PATH entry is a warning, never a silent edit of shell profiles.
10. **Windows is experimental.** The PowerShell header, README matrix and doctor
    say so until it is parsed and run on a real Windows host.

## Verification performed

| Check | Result |
|---|---|
| `sh -n scripts/install.sh` | exit 0 |
| `bash -n scripts/install.sh` | exit 0 |
| `sh scripts/install.sh --help` | exit 0, all 12 flags documented |
| `sh -s -- --help < scripts/install.sh` | exit 0 (the `curl | sh` entrypoint) |
| temp-`HOME` `--repo "$PWD" --dir … --no-setup --dry-run` | exit 0; printed the clone/sync/env/shim plan; **nothing written outside the temp root** |
| Fake-`uv` local install, temp `HOME` | cloned, checked out the tag, wrote `.env` (0600, absolute paths), wrote an executable shim, re-ran the update path |
| Backup idempotence | second run reused `.env.bak`; third created `.env.<UTC>.bak` without overwriting |
| `--uninstall` | removed shim + install dir; a planted `keepme.txt` in the data dir survived |
| Dirty tracked file / non-git dir | both refused with a clear message |
| Flag matrix | `--version v0.4.0 --profile nvidia-gpu --download --yes` → correct setup argv; `--no-download`, `--ref main`, unknown flag, conflicting `--version`+`--ref` all behave as documented |
| PowerShell | **not parse-checked** (`pwsh` absent); ASCII-only, no `$PSStyle`/`??`/`::new()`, `#requires -version 5.1`, structural bracket/here-string heuristic passed |

## Trust-model wording (mirrored in `SECURITY.md`)

Piping a URL to a shell is a trust decision the operator makes. The installer
pins a release tag by default (not the mutable `main`), performs no silent `sudo`,
sends no telemetry, and exposes `--dry-run` so the exact actions can be audited
before execution. An operator who does not want `curl | sh` can clone the tag,
read `scripts/install.sh`, and run it with `--repo <local checkout>`; the installer
is a thin wrapper around `git`, `uv` and `agent-voice setup`, not a second
installation mechanism.

## Known limitations

- `--uninstall` does not remove the data dir by design; the path is printed.
- A local `--repo` path still runs `git clone` from that path; it is offline, not
  tarball-based.
- Windows execution and PowerShell parsing remain unverified on this host and are
  labelled experimental in the README and `doctor`.
- The install directory's `.env` is created only if absent; an existing operator
  `.env` is preserved and backed up, never overwritten wholesale.
