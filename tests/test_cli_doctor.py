"""v0.3.3 CLI onboarding: `doctor` (environment gate) and `mcp-config`.

`doctor` deliberately inspects the real machine — PATH, a socket bind,
nvidia-smi, the engine registry — so every test that asserts on a row fakes the
probe instead of trusting the host. One test runs it against this checkout for
real, and skips when the box cannot satisfy doctor's hard requirements.
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
from typer.testing import CliRunner

from agent_voice.cli import app
from agent_voice.config import reset_settings_cache

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _text(result) -> str:
    """Rows go to stdout and the summary to stderr; assert on both, once.

    Click versions disagree on whether `Result.output` already folds stderr in,
    so append it only when it is missing.
    """
    combined = getattr(result, "output", "")
    stderr = getattr(result, "stderr", "")
    return combined if stderr in combined else combined + stderr


def patch_which(mapping: dict[str, str | None], monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch `shutil.which` for the named tools only; everything else stays real."""
    inherited = shutil.which

    def fake_which(name: str, *args: object, **kwargs: object) -> str | None:
        if name in mapping:
            return mapping[name]
        return inherited(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", fake_which)


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
    # No .env in this cwd: a fresh checkout. That is supported, so it is a WARN.
    monkeypatch.chdir(tmp_path)
    reset_settings_cache()

    result = runner.invoke(app, ["doctor"])
    output = _text(result)

    assert result.exit_code == 0, output
    assert "cp .env.example .env" in output
    assert "WARN" in output


# ---------------------------------------------------------------------------
# doctor: FAIL rows
# ---------------------------------------------------------------------------


def test_doctor_fails_on_an_unresolvable_default_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tools: None, free_port: int
) -> None:
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
