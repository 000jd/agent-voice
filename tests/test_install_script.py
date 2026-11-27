"""Contract tests for the one-command installers (`scripts/install.sh`, `install.ps1`).

`install.sh` is POSIX sh that people run as `curl | sh`, so the tests that matter
are cheap and audit-shaped: it parses under dash and bash, `--help` exits 0, a
temp-`HOME` `--dry-run` names every path it would write and writes nothing, and a
real local install against a local checkout plus a fake `uv` produces the shim
and an absolute `.env` while leaving the data directory outside the install.

Nothing here touches the network, installs uv, or runs a real `uv sync`: `--repo`
always points at a local path, and the one installing test puts a shell stub
named `uv` first on `PATH`. `install.ps1` is parse-checked with PowerShell when
it is present and skipped with a reason otherwise — it is explicitly labelled
experimental/unverified on a real Windows host.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"

SH_FLAGS = (
    "--dir",
    "--version",
    "--ref",
    "--repo",
    "--yes",
    "--profile",
    "--download",
    "--no-download",
    "--no-setup",
    "--uninstall",
    "--dry-run",
    "--help",
)

PS1_SWITCHES = (
    "[string]$Dir",
    "[string]$Version",
    "[string]$Ref",
    "[string]$Repo",
    "[switch]$Yes",
    "[string]$Profile",
    "[switch]$Download",
    "[switch]$NoDownload",
    "[switch]$NoSetup",
    "[switch]$Uninstall",
    "[switch]$DryRun",
    "[switch]$Help",
)

FAKE_UV_TEMPLATE = """#!/bin/sh
# Test stub: implements only `uv sync --project <dir>`, which is all the
# installer calls. It creates the entry point the installer then verifies.
project=""
while [ $# -gt 0 ]; do
    case $1 in
        --project) shift; project=$1 ;;
    esac
    shift
done
[ -n "$project" ] || { echo "fake uv: --project is required" >&2; exit 2; }
mkdir -p "$project/.venv/bin"
cat >"$project/.venv/bin/agent-voice" <<'ENTRY_POINT'
#!/bin/sh
{body}
ENTRY_POINT
chmod +x "$project/.venv/bin/agent-voice"
exit 0
"""

FAKE_UV = FAKE_UV_TEMPLATE.replace("{body}", "echo fake agent-voice")


def _fake_uv_recording(record: Path) -> str:
    """The same stub, but the generated entry point records its argv."""
    body = f"""printf '%s\\n' "$AGENT_VOICE_HOME" "$@" >'{record}'"""
    return FAKE_UV_TEMPLATE.replace("{body}", body)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    """An argv list, never a shell string."""
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        shell=False,
        env=env,
        timeout=timeout,
        check=False,
    )


def _sh() -> str:
    binary = shutil.which("sh")
    if binary is None:
        pytest.skip("no POSIX sh on PATH")
    return binary


def _git() -> str:
    binary = shutil.which("git")
    if binary is None:
        pytest.skip("git is not on PATH")
    return binary


def _temp_env(home: Path, *, extra_path: Path | None = None) -> dict[str, str]:
    """A minimal environment: the installer must only touch this HOME."""
    path = os.environ.get("PATH", "")
    if extra_path is not None:
        path = f"{extra_path}{os.pathsep}{path}"
    return {"HOME": str(home), "PATH": path, "LANG": "C.UTF-8"}


def _installer(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run([_sh(), str(INSTALL_SH), *args], env=env)


def _git_run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run([_git(), "-C", str(cwd), *args])


def _plan_line(stdout: str, label: str) -> str:
    """One whitespace-normalised `label ... value` line of the dry-run plan."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{label} "):
            return " ".join(stripped.split())
    return ""


def _make_local_checkout(root: Path, *, tag: str = "v0.6.0") -> Path:
    """A tiny local git repo with one release tag: fully offline."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("fake checkout\n", encoding="utf-8")
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    assert _git_run(root, "init", "-q").returncode == 0
    assert _git_run(root, "add", "-A").returncode == 0
    committed = _git_run(
        root,
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "user.name=agent-voice-test",
        "commit",
        "-qm",
        "init",
    )
    assert committed.returncode == 0, committed.stderr
    tagged = _git_run(root, "tag", tag)
    assert tagged.returncode == 0, tagged.stderr
    return root


# ---------------------------------------------------------------------------
# syntax and interface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_install_sh_parses(shell: str) -> None:
    binary = shutil.which(shell)
    if binary is None:
        pytest.skip(f"{shell} is not on PATH")
    assert INSTALL_SH.is_file()

    result = _run([binary, "-n", str(INSTALL_SH)])

    assert result.returncode == 0, result.stderr


def test_help_exits_zero_and_documents_every_flag() -> None:
    result = _installer("--help", env=_temp_env(Path("/nonexistent")))

    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    assert "curl -fsSL" in result.stdout
    for flag in SH_FLAGS:
        assert flag in result.stdout, flag


def test_an_unknown_flag_fails_with_a_usage_hint(tmp_path: Path) -> None:
    result = _installer("--not-a-flag", env=_temp_env(tmp_path))

    assert result.returncode == 1
    assert "unknown argument: --not-a-flag" in result.stderr
    assert "--help" in result.stderr


def test_install_sh_is_posix_sh_not_bash() -> None:
    text = INSTALL_SH.read_text("utf-8")
    assert text.startswith("#!/bin/sh\n")
    # `set -eu` on every POSIX shell; pipefail only when bash is detected.
    assert "set -eu" in text
    assert re.search(r"if \[ -n \"\$\{BASH_VERSION:-\}\" \]; then", text)


# ---------------------------------------------------------------------------
# --dry-run: the audit path
# ---------------------------------------------------------------------------


def test_dry_run_plan_names_every_path_and_writes_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install = tmp_path / "install"
    data = tmp_path / "agent-voice-data"
    shim = home / ".local" / "bin" / "agent-voice"
    repo_before = sorted(path.name for path in REPO_ROOT.iterdir())

    result = _installer(
        "--repo",
        str(REPO_ROOT),
        "--dir",
        str(install),
        "--no-setup",
        "--version",
        "v0.6.0",
        "--dry-run",
        env=_temp_env(home),
    )

    assert result.returncode == 0, result.stderr
    plan = result.stdout
    assert "DRY RUN (nothing will be written)" in plan
    for label in ("install dir", "data dir", "engines dir", "shim"):
        assert label in plan, label
    assert str(install) in plan
    assert str(data) in plan
    assert str(install / "engines") in plan
    assert str(shim) in plan
    assert _plan_line(plan, "target") == "target v0.6.0 (--version)"
    assert "Skipping 'agent-voice setup' (--no-setup)" in plan
    assert "Dry run complete: no changes were made." in plan
    # Every action is printed as an argv, and the real thing never runs.
    assert f"+ git clone {REPO_ROOT} {install}" in plan
    assert f"+ mkdir -p {tmp_path}" in plan
    assert "--project" in plan

    # Nothing was written anywhere, and the repo is untouched.
    assert not install.exists()
    assert not data.exists()
    assert not (home / ".local").exists()
    assert sorted(path.name for path in REPO_ROOT.iterdir()) == repo_before


def test_dry_run_warns_that_data_is_a_sibling_of_the_install(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    result = _installer(
        "--repo",
        str(REPO_ROOT),
        "--dir",
        str(tmp_path / "nested" / "install"),
        "--no-setup",
        "--version",
        "v0.6.0",
        "--dry-run",
        env=_temp_env(home),
    )

    assert result.returncode == 0, result.stderr
    assert str(tmp_path / "nested" / "agent-voice-data") in result.stdout


def test_dry_run_resolves_the_latest_local_tag(tmp_path: Path) -> None:
    """Tag pinning is the default: never `main` unless `--ref main` is explicit."""
    tags = [
        line.strip()
        for line in _git_run(REPO_ROOT, "tag", "--list").stdout.splitlines()
        if re.fullmatch(r"v\d+\.\d+\.\d+", line.strip())
    ]
    if not tags:
        pytest.skip("this checkout has no vX.Y.Z tag for the installer to resolve")
    newest = max(tags, key=lambda tag: tuple(int(part) for part in tag[1:].split(".")))
    home = tmp_path / "home"
    home.mkdir()

    result = _installer(
        "--repo",
        str(REPO_ROOT),
        "--dir",
        str(tmp_path / "install"),
        "--no-setup",
        "--dry-run",
        env=_temp_env(home),
    )

    assert result.returncode == 0, result.stderr
    assert _plan_line(result.stdout, "target") == f"target {newest} (latest release tag)"


# ---------------------------------------------------------------------------
# uninstall safety
# ---------------------------------------------------------------------------


def test_uninstall_dry_run_keeps_the_data_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    install = tmp_path / "install"
    install.mkdir()
    (install / "ENGINE").write_text("x", encoding="utf-8")
    data = tmp_path / "agent-voice-data"
    data.mkdir()
    voice = data / "voices" / "voice.wav"
    voice.parent.mkdir()
    voice.write_text("keep me", encoding="utf-8")

    result = _installer(
        "--dir",
        str(install),
        "--uninstall",
        "--dry-run",
        env=_temp_env(home),
    )

    assert result.returncode == 0, result.stderr
    assert _plan_line(result.stdout, "data:") == f"data: {data} (kept)"
    assert f"Data directory kept: {data}" in result.stdout
    assert "Dry run: nothing was removed." in result.stdout
    # The plan names what it would delete, and deletes nothing.
    assert f"+ rm -rf {install}" in result.stdout
    assert install.is_dir()
    assert voice.read_text("utf-8") == "keep me"


# ---------------------------------------------------------------------------
# a real local install, with a stubbed uv (offline, no weights, no setup)
# ---------------------------------------------------------------------------


def test_local_install_writes_the_shim_and_an_absolute_env(tmp_path: Path) -> None:
    source = _make_local_checkout(tmp_path / "src")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake_uv = fakebin / "uv"
    fake_uv.write_text(FAKE_UV, encoding="utf-8")
    fake_uv.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    install = tmp_path / "install"
    data = tmp_path / "agent-voice-data"
    shim = home / ".local" / "bin" / "agent-voice"
    env_file = install / ".env"

    result = _installer(
        "--repo",
        str(source),
        "--dir",
        str(install),
        "--no-setup",
        "--yes",
        env=_temp_env(home, extra_path=fakebin),
    )

    assert result.returncode == 0, result.stderr
    assert "agent-voice v0.6.0 installed." in result.stdout
    # uv was the stub: nothing was downloaded and no real venv was created.
    assert str(fake_uv) in result.stdout
    assert (install / ".venv" / "bin" / "agent-voice").is_file()

    assert shim.is_file()
    assert os.access(shim, os.X_OK)
    shim_text = shim.read_text("utf-8")
    assert f"AGENT_VOICE_HOME='{install}'" in shim_text
    assert f"exec '{install}/.venv/bin/agent-voice' \"$@\"" in shim_text

    assert env_file.is_file()
    assert env_file.stat().st_mode & 0o777 == 0o600
    body = env_file.read_text("utf-8")
    assert f"AGENT_VOICE_HOME={install}" in body
    assert f"DATA_DIR={data}" in body
    assert f"ENGINES_DIR={install / 'engines'}" in body

    # Data is a sibling of the install dir, created, and never inside it.
    assert data.is_dir()
    assert Path(install / "..").resolve() == tmp_path
    assert not str(data).startswith(f"{install}{os.sep}")

    # Everything the run created is inside the temp root, and nothing else is.
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        ["src", "fakebin", "home", "install", "agent-voice-data"]
    )
    assert sorted(path.name for path in source.iterdir()) == [".git", "README.md", "app.py"]
    assert _git_run(source, "status", "--porcelain").stdout == ""


def test_local_install_forwards_setup_flags_to_the_real_cli(tmp_path: Path) -> None:
    """Without `--no-setup` the installer execs the venv entry point with the flags.

    The stub `uv` writes an entry point that records its argv, so this asserts the
    exact forwarding contract (`--yes`, `--profile`, `--download`, and
    `AGENT_VOICE_HOME` in the child environment) without running setup.
    """
    source = _make_local_checkout(tmp_path / "src")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    record = tmp_path / "setup-argv.txt"
    fake_uv = fakebin / "uv"
    fake_uv.write_text(_fake_uv_recording(record), encoding="utf-8")
    fake_uv.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    install = tmp_path / "install"

    result = _installer(
        "--repo",
        str(source),
        "--dir",
        str(install),
        "--yes",
        "--profile",
        "cpu-only",
        "--download",
        env=_temp_env(home, extra_path=fakebin),
    )

    assert result.returncode == 0, result.stderr
    assert "Running 'agent-voice setup'" in result.stdout
    assert record.is_file()
    forwarded = record.read_text("utf-8").splitlines()
    assert forwarded[0] == str(install)
    assert forwarded[1:] == [
        "setup",
        "--yes",
        "--profile",
        "cpu-only",
        "--download",
    ]


# ---------------------------------------------------------------------------
# install.ps1: parse-checked only when PowerShell is available
# ---------------------------------------------------------------------------


def test_install_ps1_parses_when_powershell_is_available() -> None:
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if pwsh is None:
        pytest.skip(
            "no pwsh/powershell on PATH: install.ps1 is labelled experimental/"
            "unverified on real Windows and can only be parse-checked with PowerShell"
        )
    assert INSTALL_PS1.is_file()
    script = "; ".join(
        (
            "$tokens = $null",
            "$errors = $null",
            "[System.Management.Automation.Language.Parser]::ParseFile("
            f"'{INSTALL_PS1}', [ref]$tokens, [ref]$errors) | Out-Null",
            "if ($errors -and $errors.Count -gt 0) { "
            "$errors | ForEach-Object { Write-Host $_.Message }; exit 1 }",
            "exit 0",
        )
    )

    result = _run([pwsh, "-NoProfile", "-NonInteractive", "-Command", script])

    assert result.returncode == 0, result.stdout + result.stderr


def test_install_ps1_labels_itself_experimental_and_mirrors_the_flags() -> None:
    text = INSTALL_PS1.read_text("utf-8")

    assert "EXPERIMENTAL / NOT VERIFIED ON REAL WINDOWS" in text
    for switch in PS1_SWITCHES:
        assert switch in text, switch
    # The same uv installer and the same no-elevation promise as install.sh.
    assert "https://astral.sh/uv/install.ps1" in text
    assert "Read-Host" in text  # consent, not a silent install


def test_install_ps1_is_ascii_only() -> None:
    """Windows PowerShell 5.1 mis-decodes a UTF-8 file without a BOM."""
    raw = INSTALL_PS1.read_bytes()

    assert raw.isascii(), "install.ps1 must stay ASCII-only"
    assert not raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe")), "no BOM"
