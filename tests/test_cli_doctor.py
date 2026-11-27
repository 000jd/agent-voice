"""v0.3.3/v0.4.0 CLI onboarding: `doctor` (environment gate), `serve` and `mcp-config`.

`doctor` deliberately inspects the real machine — PATH, a socket bind,
nvidia-smi, the engine registry — so every test that asserts on a row fakes the
probe instead of trusting the host. One test runs it against this checkout for
real, and skips when the box cannot satisfy doctor's hard requirements.

`serve` is never actually started: uvicorn.run is replaced, so these tests assert
what the command would bind (and whether it refuses at all) without a daemon.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from pydantic import ValidationError
from typer.testing import CliRunner

import agent_voice.cli as cli
from agent_voice.cli import app
from agent_voice.config import Settings, get_settings, reset_settings_cache

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_settings_cache() -> Iterator[None]:
    """Every test starts and ends with a fresh settings cache.

    Several tests override AGENT_VOICE_HOST/PORT or chdir to a directory with its
    own `.env`; `get_settings` is `lru_cache`d, so a stale Settings object would
    leak both ways without this.
    """
    reset_settings_cache()
    yield
    reset_settings_cache()


def _text(result) -> str:
    """Rows go to stdout and the summary to stderr; assert on both, once.

    Click versions disagree on whether `Result.output` already folds stderr in,
    so append it only when it is missing.
    """
    combined = getattr(result, "output", "")
    stderr = getattr(result, "stderr", "")
    return combined if stderr in combined else combined + stderr


def _assert_exit(result, code: int) -> str:
    """Assert the command exited with `code` on purpose, not by raising.

    Click reports an escaped exception as exit_code 1 as well, so an
    `isinstance(SystemExit | None)` check is what proves `typer.Exit` (or a clean
    return) was reached instead of a traceback.
    """
    output = _text(result)
    assert result.exit_code == code, output
    assert isinstance(result.exception, SystemExit) or result.exception is None, (
        f"expected typer.Exit, raised {result.exception!r}"
    )
    assert "Traceback (most recent call last)" not in output, output
    return output


def patch_which(mapping: dict[str, str | None], monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch `shutil.which` for the named tools only; everything else stays real."""
    inherited = shutil.which

    def fake_which(name: str, *args: object, **kwargs: object) -> str | None:
        if name in mapping:
            return mapping[name]
        return inherited(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", fake_which)


def fake_uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace `uvicorn.run`: record what `serve` would bind instead of serving."""
    seen: dict[str, object] = {}

    def fake_run(target: str, **kwargs: object) -> None:
        seen["target"] = target
        seen.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return seen


def forbid_uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if `serve` ever reaches uvicorn (it must refuse first)."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("uvicorn.run must not be reached when the bind is refused")

    monkeypatch.setattr(uvicorn, "run", forbidden)


def patch_nvidia_smi(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = "Fake GPU, 8192 MiB",
    stderr: str = "",
) -> list[list[str]]:
    """Fake nvidia-smi. Returns the argv lists it was called with."""
    patch_which({"nvidia-smi": "/fake/nvidia-smi"}, monkeypatch)
    seen: list[list[str]] = []

    def fake_run(argv, *args: object, **kwargs: object):
        # House rule: an argv list, never a shell string.
        assert isinstance(argv, list), argv
        assert kwargs.get("shell") is False
        if list(argv)[0] == "/fake/nvidia-smi":
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
        # EngineRegistry also probes each installed backend's version through a
        # subprocess (`<venv python> -c ...`) and decodes its stdout as bytes.
        return subprocess.CompletedProcess(argv, 0, b"null", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv/ffmpeg/ffprobe present, no GPU: every tool row is decided by the test."""
    patch_which(
        {
            "uv": "/fake/uv",
            "ffmpeg": "/fake/ffmpeg",
            "ffprobe": "/fake/ffprobe",
            "nvidia-smi": None,
        },
        monkeypatch,
    )


@pytest.fixture
def free_port(monkeypatch: pytest.MonkeyPatch) -> int:
    """Pin settings to a loopback port nothing listens on, so `port` is stable."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("AGENT_VOICE_HOST", "127.0.0.1")
    monkeypatch.setenv("AGENT_VOICE_PORT", str(port))
    reset_settings_cache()
    return port


@pytest.fixture
def occupied_port(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """Hold a listening socket on a loopback port, so a bind cannot succeed."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    monkeypatch.setenv("AGENT_VOICE_HOST", "127.0.0.1")
    monkeypatch.setenv("AGENT_VOICE_PORT", str(port))
    reset_settings_cache()
    try:
        yield port
    finally:
        listener.close()


# ---------------------------------------------------------------------------
# doctor: hard requirements
# ---------------------------------------------------------------------------


def test_doctor_fails_when_ffmpeg_is_missing(
    monkeypatch: pytest.MonkeyPatch, tools: None, free_port: int
) -> None:
    patch_which({"ffmpeg": None, "ffprobe": None}, monkeypatch)

    result = runner.invoke(app, ["doctor"])
    output = _text(result)

    assert result.exit_code == 1, output
    assert "ffmpeg" in output
    assert "ffprobe" in output
    # A FAIL row carries a one-line fix.
    assert "fix:" in output
    assert "install ffmpeg" in output


def test_doctor_fails_when_uv_is_missing(
    monkeypatch: pytest.MonkeyPatch, tools: None, free_port: int
) -> None:
    patch_which({"uv": None}, monkeypatch)

    result = runner.invoke(app, ["doctor"])
    output = _text(result)

    assert result.exit_code == 1, output
    assert "uv" in output
    assert "install uv" in output


# ---------------------------------------------------------------------------
# doctor: warnings never fail the run
# ---------------------------------------------------------------------------


def test_doctor_warns_without_a_gpu_but_still_exits_zero(tools: None, free_port: int) -> None:
    result = runner.invoke(app, ["doctor"])
    output = _text(result)

    assert result.exit_code == 0, output
    assert "mock/CPU only" in output
    assert "WARN" in output


def test_doctor_reports_the_interpreter_it_runs_under(tools: None, free_port: int) -> None:
    import platform

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, _text(result)
    assert "python" in result.output
    assert platform.python_version() in result.output


def test_doctor_passes_when_nvidia_smi_answers(
    monkeypatch: pytest.MonkeyPatch, tools: None, free_port: int
) -> None:
    argv = patch_nvidia_smi(monkeypatch, stdout="Fake GPU, 8192 MiB\n")

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, _text(result)
    assert "Fake GPU, 8192 MiB" in result.output
    assert argv == [
        [
            "/fake/nvidia-smi",
            "--query-gpu=name,memory.total",
            "--format=csv,noheader",
        ]
    ]


def test_doctor_warns_when_nvidia_smi_fails(
    monkeypatch: pytest.MonkeyPatch, tools: None, free_port: int
) -> None:
    patch_nvidia_smi(monkeypatch, returncode=9, stdout="", stderr="driver not loaded")

    result = runner.invoke(app, ["doctor"])
    output = _text(result)

    assert result.exit_code == 0, output
    assert "driver not loaded" in output


def test_doctor_warns_when_dotenv_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tools: None, free_port: int
) -> None:
    # No .env in this home: a fresh checkout. That is supported, so it is a WARN.
    # Setting both keeps the test working before and after the v0.6.0 home change.
    monkeypatch.setenv("AGENT_VOICE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    reset_settings_cache()

    result = runner.invoke(app, ["doctor"])
    output = _text(result)

    assert result.exit_code == 0, output
    assert "cp .env.example .env" in output
    assert "WARN" in output
    # M4: absence is its own status text, but the effective engine is still judged.
    assert "absent, built-in defaults apply" in output
    assert "AGENT_VOICE_DEFAULT_ENGINE=mock (installed)" in output


def test_doctor_fails_on_a_ghost_engine_from_the_environment_without_a_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tools: None, free_port: int
) -> None:
    """M4: no `.env` must not skip the engine check — the shell can set it."""
    monkeypatch.setenv("AGENT_VOICE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # a fresh checkout: no `.env` here
    monkeypatch.setenv("AGENT_VOICE_DEFAULT_ENGINE", "ghost_engine")
    reset_settings_cache()

    result = runner.invoke(app, ["doctor"])
    output = _text(result)

    assert result.exit_code == 1, output
    assert "absent, built-in defaults apply" in output
    assert "AGENT_VOICE_DEFAULT_ENGINE=ghost_engine" in output
    assert "fix:" in output


def test_doctor_env_row_reports_presence_and_the_effective_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tools: None, free_port: int
) -> None:
    # Create the .env this test needs: a checkout-local file is gitignored and
    # absent on a fresh clone, which made this test pass only on a dev machine.
    monkeypatch.setenv("AGENT_VOICE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("AGENT_VOICE_DEFAULT_ENGINE=mock\n", "utf-8")
    # The shell variable would win over the file and mask it.
    monkeypatch.delenv("AGENT_VOICE_DEFAULT_ENGINE", raising=False)
    reset_settings_cache()

    result = runner.invoke(app, ["doctor"])
    output = _text(result)

    assert result.exit_code == 0, output
    assert "present; AGENT_VOICE_DEFAULT_ENGINE=" in output
    assert "(installed)" in output


# ---------------------------------------------------------------------------
# doctor: FAIL rows
# ---------------------------------------------------------------------------


def test_doctor_fails_on_an_unresolvable_default_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tools: None, free_port: int
) -> None:
    monkeypatch.setenv("AGENT_VOICE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("AGENT_VOICE_DEFAULT_ENGINE=ghost_engine\n", "utf-8")
    monkeypatch.delenv("AGENT_VOICE_DEFAULT_ENGINE", raising=False)
    reset_settings_cache()

    result = runner.invoke(app, ["doctor"])
    output = _text(result)

    assert result.exit_code == 1, output
    assert "ghost_engine" in output
    assert "AGENT_VOICE_DEFAULT_ENGINE" in output
    assert "fix:" in output


def test_doctor_fails_when_no_engine_is_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tools: None, free_port: int
) -> None:
    monkeypatch.setenv("ENGINES_DIR", str(tmp_path / "engines"))
    reset_settings_cache()

    result = runner.invoke(app, ["doctor"])
    output = _text(result)

    assert result.exit_code == 1, output
    assert "no engine installed" in output
    assert "agent-voice engines install" in output


def test_doctor_reports_an_unusable_engines_dir_as_a_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tools: None, free_port: int
) -> None:
    # A file where the engines directory should be: the doctor must explain it
    # instead of dying with a traceback.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", "utf-8")
    monkeypatch.setenv("ENGINES_DIR", str(blocker / "engines"))
    reset_settings_cache()

    result = runner.invoke(app, ["doctor"])
    output = _text(result)

    assert result.exit_code == 1, output
    assert "not usable" in output
    assert str(blocker / "engines") in output


def test_doctor_fails_when_the_data_dir_is_unusable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tools: None, free_port: int
) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("", "utf-8")
    monkeypatch.setenv("DATA_DIR", str(blocker / "data"))
    reset_settings_cache()

    result = runner.invoke(app, ["doctor"])
    output = _text(result)

    assert result.exit_code == 1, output
    assert "inbox" in output
    assert "not usable" in output


def test_doctor_fails_when_the_port_is_taken_by_something_else(
    monkeypatch: pytest.MonkeyPatch, tools: None, occupied_port: int
) -> None:
    def refuse(url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", refuse)

    result = runner.invoke(app, ["doctor"])
    output = _text(result)

    assert result.exit_code == 1, output
    assert f"127.0.0.1:{occupied_port}" in output
    assert "in use" in output


def test_doctor_treats_a_running_daemon_as_a_pass(
    monkeypatch: pytest.MonkeyPatch, tools: None, occupied_port: int
) -> None:
    def healthy(url: str, **kwargs: object) -> httpx.Response:
        assert url.endswith("/health"), url
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(httpx, "get", healthy)

    result = runner.invoke(app, ["doctor"])
    output = _text(result)

    assert result.exit_code == 0, output
    assert "healthy daemon" in output


# ---------------------------------------------------------------------------
# doctor: a broken config is a row, not a traceback (M3)
# ---------------------------------------------------------------------------


def test_doctor_reports_an_invalid_dotenv_as_a_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tools: None
) -> None:
    """`AGENT_VOICE_PORT=abc` in .env used to traceback out of get_settings()."""
    monkeypatch.setenv("AGENT_VOICE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("AGENT_VOICE_PORT=abc\n", "utf-8")
    # The shell variable would win over the file and mask the bad value.
    monkeypatch.delenv("AGENT_VOICE_PORT", raising=False)
    reset_settings_cache()

    result = runner.invoke(app, ["doctor"])
    output = _assert_exit(result, 1)

    assert "config" in output
    assert "FAIL" in output
    assert "AGENT_VOICE_PORT" in output
    assert "fix .env" in output
    # The remaining rows still print, on built-in defaults.
    assert "inbox" in output
    assert "port" in output


def test_doctor_reports_an_invalid_enum_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tools: None, free_port: int
) -> None:
    """A bad enum value is the same class of failure as a non-numeric port."""

    def broken_settings() -> Settings:
        return Settings(AGENT_VOICE_TOOLSET="ghost_toolset", _env_file=None)

    with pytest.raises(ValidationError):
        broken_settings()

    monkeypatch.setattr(cli, "get_settings", broken_settings)

    result = runner.invoke(app, ["doctor"])
    output = _assert_exit(result, 1)

    assert "config" in output
    assert "AGENT_VOICE_TOOLSET" in output
    assert "fix .env" in output


# ---------------------------------------------------------------------------
# doctor: the port probe is address-family aware (M5)
# ---------------------------------------------------------------------------


def _ipv6_loopback_available() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True


def _free_port(family: socket.AddressFamily, host: str) -> int:
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def test_doctor_reports_a_free_ipv6_loopback_port_as_pass(
    monkeypatch: pytest.MonkeyPatch, tools: None
) -> None:
    """AGENT_VOICE_HOST=::1 is a supported loopback bind, not an AF_INET port."""
    if not _ipv6_loopback_available():
        pytest.skip("this machine has no IPv6 loopback")
    port = _free_port(socket.AF_INET6, "::1")
    monkeypatch.setenv("AGENT_VOICE_HOST", "::1")
    monkeypatch.setenv("AGENT_VOICE_PORT", str(port))
    reset_settings_cache()

    result = runner.invoke(app, ["doctor"])
    output = _text(result)

    assert result.exit_code == 0, output
    assert f"::1:{port} is free" in output


def test_doctor_fails_when_an_ipv6_loopback_port_is_held(
    monkeypatch: pytest.MonkeyPatch, tools: None
) -> None:
    if not _ipv6_loopback_available():
        pytest.skip("this machine has no IPv6 loopback")

    listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    listener.bind(("::1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])
    monkeypatch.setenv("AGENT_VOICE_HOST", "::1")
    monkeypatch.setenv("AGENT_VOICE_PORT", str(port))
    reset_settings_cache()

    def refuse(url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", refuse)
    try:
        result = runner.invoke(app, ["doctor"])
    finally:
        listener.close()
    output = _text(result)

    assert result.exit_code == 1, output
    assert f"::1:{port}" in output
    assert "in use" in output


# ---------------------------------------------------------------------------
# doctor: the real checkout (the release gate)
# ---------------------------------------------------------------------------


def test_doctor_is_green_on_this_checkout(free_port: int) -> None:
    missing = [tool for tool in ("uv", "ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"doctor requires {missing}, which this machine lacks")

    result = runner.invoke(app, ["doctor"])
    output = _text(result)

    assert result.exit_code == 0, output
    assert "ffmpeg" in output
    assert "inbox" in output
    assert "PASS" in output
    assert "FAIL" not in output


def test_doctor_never_imports_torch(tools: None, free_port: int) -> None:
    if "torch" in sys.modules:  # pragma: no cover - depends on collection order
        pytest.skip("torch was already imported by another test module")

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, _text(result)
    assert "torch" not in sys.modules


# ---------------------------------------------------------------------------
# mcp-config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client", ["claude-code", "claude-desktop", "cursor", "command-code"])
def test_mcp_config_emits_a_claude_style_snippet(client: str) -> None:
    result = runner.invoke(app, ["mcp-config", "--client", client])

    assert result.exit_code == 0, _text(result)
    payload = json.loads(result.output)
    assert set(payload) == {"mcpServers"}
    server = payload["mcpServers"]["agent-voice"]
    assert server["command"] == "uv"
    assert server["args"] == ["run", "--directory", str(REPO_ROOT), "agent-voice", "mcp"]
    assert server["env"] == {"AGENT_VOICE_CLIENT_ID": client}


def test_mcp_config_claude_code_names_the_absolute_repo_root() -> None:
    result = runner.invoke(app, ["mcp-config", "--client", "claude-code"])
    output = _text(result)

    assert result.exit_code == 0, output
    assert str(REPO_ROOT) in output, output
    assert "agent-voice" in output


def test_mcp_config_vscode_uses_servers_and_stdio() -> None:
    result = runner.invoke(app, ["mcp-config", "--client", "vscode"])

    assert result.exit_code == 0, _text(result)
    payload = json.loads(result.output)
    assert set(payload) == {"servers"}
    server = payload["servers"]["agent-voice"]
    assert server["type"] == "stdio"
    assert server["command"] == "uv"
    assert server["args"] == ["run", "--directory", str(REPO_ROOT), "agent-voice", "mcp"]
    assert server["env"] == {"AGENT_VOICE_CLIENT_ID": "vscode"}


def test_mcp_config_rejects_an_unknown_client() -> None:
    result = runner.invoke(app, ["mcp-config", "--client", "notepad"])

    assert result.exit_code != 0
    assert "notepad" in _text(result)
    assert "claude-code" in _text(result)


def test_mcp_config_requires_a_client() -> None:
    result = runner.invoke(app, ["mcp-config"])

    assert result.exit_code != 0
    assert "--client" in _text(result)


# ---------------------------------------------------------------------------
# serve: the bind safety gate is judged on the *effective* address (H1/L7)
# ---------------------------------------------------------------------------


def test_serve_refuses_a_non_loopback_host_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`serve --host 0.0.0.0` must not slip past the check on the configured host."""
    forbid_uvicorn_run(monkeypatch)
    monkeypatch.setenv("AGENT_VOICE_HOST", "127.0.0.1")  # config itself is safe
    monkeypatch.delenv("AGENT_VOICE_API_KEY", raising=False)
    reset_settings_cache()

    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    output = _assert_exit(result, 1)

    assert "AGENT_VOICE_API_KEY" in output
    assert "0.0.0.0" in output
    # Nothing was started and nothing was left behind.
    assert not (get_settings().data_dir / "daemon.pid").exists()


def test_serve_allows_a_non_loopback_host_when_a_key_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = fake_uvicorn_run(monkeypatch)
    monkeypatch.setenv("AGENT_VOICE_HOST", "127.0.0.1")
    monkeypatch.setenv("AGENT_VOICE_API_KEY", "secret-key")
    reset_settings_cache()

    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    _assert_exit(result, 0)

    assert seen["host"] == "0.0.0.0"
    assert seen["target"] == "agent_voice.api.app:create_app"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_serve_allows_a_loopback_host_override(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """The override is what is judged: a LAN-configured box may be narrowed to loopback."""
    seen = fake_uvicorn_run(monkeypatch)
    monkeypatch.setenv("AGENT_VOICE_HOST", "0.0.0.0")  # unsafe as configured
    monkeypatch.delenv("AGENT_VOICE_API_KEY", raising=False)
    reset_settings_cache()

    result = runner.invoke(app, ["serve", "--host", host, "--port", "8123"])
    _assert_exit(result, 0)

    assert seen["host"] == host
    assert seen["port"] == 8123


def test_serve_forwards_port_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """L7: `--port 0` means "any free port", so `or` must not fall back to 8000."""
    seen = fake_uvicorn_run(monkeypatch)
    monkeypatch.setenv("AGENT_VOICE_HOST", "127.0.0.1")
    monkeypatch.setenv("AGENT_VOICE_PORT", "8000")
    reset_settings_cache()

    result = runner.invoke(app, ["serve", "--port", "0"])
    _assert_exit(result, 0)

    assert seen["port"] == 0
    # No --host given: the configured host is used verbatim.
    assert seen["host"] == "127.0.0.1"
    assert seen["reload"] is False
