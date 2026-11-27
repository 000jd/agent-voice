"""v0.6.0 home anchoring: one resolved home, any-CWD CLI, prefixed aliases.

``config.default_home()`` is the single answer to "where does this install live?".
It is ``AGENT_VOICE_HOME`` when set, otherwise the checkout that holds both
``pyproject.toml`` and ``engines/`` — never the process's current directory. That
is what makes ``agent-voice engines list`` produce the same list from any CWD
without creating ``data/`` or ``engines/`` in the caller's working directory.

Everything here is hermetic: the tests point ``DATA_DIR``/``ENGINES_DIR`` (or
``AGENT_VOICE_HOME``) at ``tmp_path`` unless the point of the test is the real
checkout, and ``get_settings`` is cache-cleared around every test because it is
``lru_cache``d.

The regression section at the bottom covers a real v0.6.0 bug: with
``populate_by_name=True`` the standard ``HOME``/``PORT``/``HOST``/``DEVICE``
environment variables matched the field names and silently re-homed the install
or changed its bind address. The fix renamed the field to ``agent_home`` and maps
explicit constructor kwargs only, so those names must stay inert.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from agent_voice.cli import app
from agent_voice.config import (
    Settings,
    default_home,
    get_settings,
    reset_settings_cache,
    settings_env_file,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_ENGINES = REPO_ROOT / "engines"
runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_settings_cache() -> Iterator[None]:
    """`get_settings` is `lru_cache`d, so every home override needs a fresh call."""
    reset_settings_cache()
    yield
    reset_settings_cache()


def _write_env(home: Path, text: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / ".env"
    path.write_text(text, "utf-8")
    return path


def _engines_list() -> list[dict]:
    """Run `agent-voice engines list` and return the parsed payload."""
    result = runner.invoke(app, ["engines", "list"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


# ---------------------------------------------------------------------------
# default_home(): the checkout, never the CWD
# ---------------------------------------------------------------------------


def test_default_home_is_the_checkout_from_any_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AGENT_VOICE_HOME", raising=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert default_home() == REPO_ROOT
    assert settings_env_file() == REPO_ROOT / ".env"
    # Discovery is a read of this package's own location: nothing was created.
    assert list(elsewhere.iterdir()) == []


def test_agent_voice_home_is_absolute_and_wins_over_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENT_VOICE_HOME", str(home))
    monkeypatch.chdir(tmp_path)

    assert default_home() == home.resolve()
    assert settings_env_file() == home.resolve() / ".env"


# ---------------------------------------------------------------------------
# `<home>/.env` is the one config file
# ---------------------------------------------------------------------------


def test_get_settings_reads_the_home_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    env_path = _write_env(home, "AGENT_VOICE_PORT=8231\nCHUNK_CHARS=111\n")
    monkeypatch.setenv("AGENT_VOICE_HOME", str(home))
    # An inherited shell value would win over the file and mask it.
    monkeypatch.delenv("AGENT_VOICE_PORT", raising=False)
    monkeypatch.delenv("CHUNK_CHARS", raising=False)

    settings = get_settings()

    assert settings.home == home.resolve()
    assert settings.config_path == env_path.resolve()
    assert settings.port == 8231
    assert settings.chunk_chars == 111


def test_agent_voice_home_overrides_which_dotenv_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_env(first, "AGENT_VOICE_PORT=8100\n")
    _write_env(second, "AGENT_VOICE_PORT=8200\n")
    monkeypatch.delenv("AGENT_VOICE_PORT", raising=False)

    monkeypatch.setenv("AGENT_VOICE_HOME", str(first))
    reset_settings_cache()
    assert get_settings().port == 8100

    monkeypatch.setenv("AGENT_VOICE_HOME", str(second))
    reset_settings_cache()
    assert get_settings().port == 8200


def test_relative_data_and_engines_dirs_resolve_under_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("AGENT_VOICE_HOME", str(home))
    monkeypatch.setenv("DATA_DIR", "mydata")
    monkeypatch.setenv("ENGINES_DIR", "myengines")
    # A CWD that must not be consulted: resolving against it would put both paths
    # under `elsewhere` instead of the home, which is exactly the v0.5 bug.
    monkeypatch.chdir(elsewhere)

    settings = get_settings()

    assert settings.data_dir == (home / "mydata").resolve()
    assert settings.engines_dir == (home / "myengines").resolve()
    assert settings.data_dir.parent == settings.home
    assert list(elsewhere.iterdir()) == []


def test_legacy_relative_dotenv_paths_resolve_under_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pre-v0.6 `.env` wrote `DATA_DIR=./data`; that must mean `<home>/data`."""
    home = tmp_path / "home"
    _write_env(home, "DATA_DIR=./data\nENGINES_DIR=./engines\n")
    monkeypatch.setenv("AGENT_VOICE_HOME", str(home))
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("ENGINES_DIR", raising=False)

    settings = get_settings()

    assert settings.data_dir == (home / "data").resolve()
    assert settings.engines_dir == (home / "engines").resolve()


def test_absolute_paths_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "elsewhere" / "data"
    engines = tmp_path / "elsewhere" / "engines"
    monkeypatch.setenv("AGENT_VOICE_HOME", str(home))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("ENGINES_DIR", str(engines))

    settings = get_settings()

    assert settings.data_dir == data.resolve()
    assert settings.engines_dir == engines.resolve()
    assert home.resolve() not in settings.data_dir.parents


# ---------------------------------------------------------------------------
# Prefixed aliases
# ---------------------------------------------------------------------------


def test_prefixed_aliases_are_honoured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_VOICE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_VOICE_DATA_DIR", str(tmp_path / "agent-data"))
    monkeypatch.setenv("AGENT_VOICE_ENGINES_DIR", str(tmp_path / "agent-engines"))
    monkeypatch.setenv("AGENT_VOICE_MCP_BASE_PATH", str(tmp_path / "agent-mcp"))
    for name in ("DATA_DIR", "ENGINES_DIR", "MCP_BASE_PATH"):
        monkeypatch.delenv(name, raising=False)

    settings = get_settings()

    assert settings.data_dir == (tmp_path / "agent-data").resolve()
    assert settings.engines_dir == (tmp_path / "agent-engines").resolve()
    assert settings.mcp_base_path == str(tmp_path / "agent-mcp")


def test_prefixed_aliases_win_over_the_unprefixed_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`AGENT_VOICE_*` wins when both spellings are set — the documented rule."""
    monkeypatch.setenv("AGENT_VOICE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "plain-data"))
    monkeypatch.setenv("AGENT_VOICE_DATA_DIR", str(tmp_path / "prefixed-data"))
    monkeypatch.setenv("ENGINES_DIR", str(tmp_path / "plain-engines"))
    monkeypatch.setenv("AGENT_VOICE_ENGINES_DIR", str(tmp_path / "prefixed-engines"))
    monkeypatch.setenv("MCP_BASE_PATH", str(tmp_path / "plain-mcp"))
    monkeypatch.setenv("AGENT_VOICE_MCP_BASE_PATH", str(tmp_path / "prefixed-mcp"))

    settings = get_settings()

    assert settings.data_dir == (tmp_path / "prefixed-data").resolve()
    assert settings.engines_dir == (tmp_path / "prefixed-engines").resolve()
    assert settings.mcp_base_path == str(tmp_path / "prefixed-mcp")


def test_unprefixed_names_still_work_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "plain-data"))
    monkeypatch.setenv("ENGINES_DIR", str(tmp_path / "plain-engines"))
    monkeypatch.setenv("MCP_BASE_PATH", str(tmp_path / "plain-mcp"))
    for name in (
        "AGENT_VOICE_DATA_DIR",
        "AGENT_VOICE_ENGINES_DIR",
        "AGENT_VOICE_MCP_BASE_PATH",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.data_dir == (tmp_path / "plain-data").resolve()
    assert settings.engines_dir == (tmp_path / "plain-engines").resolve()
    assert settings.mcp_base_path == str(tmp_path / "plain-mcp")


# ---------------------------------------------------------------------------
# `populate_by_name` and the standard-environment regression
# ---------------------------------------------------------------------------


def test_settings_accept_field_names_as_constructor_kwargs() -> None:
    """`Settings(api_key="x")` works without `populate_by_name`."""
    assert Settings(api_key="x", _env_file=None).api_key == "x"
    assert Settings(host="0.0.0.0", port=9001, _env_file=None).port == 9001


def test_standard_environment_variables_do_not_leak_into_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The v0.6.0 regression: HOME/PORT/HOST/DEVICE/API_KEY must stay inert.

    With ``populate_by_name=True`` a pydantic-settings model also accepts the
    *field* names from the environment, so a plain ``HOME`` re-homed the install,
    ``PORT`` moved the daemon and ``DEVICE`` switched every engine. The fix is
    ``agent_home`` plus a constructor-kwargs-only name map; this test is the
    tripwire if either half is undone.
    """
    monkeypatch.delenv("AGENT_VOICE_HOME", raising=False)
    for name in (
        "AGENT_VOICE_HOST",
        "AGENT_VOICE_PORT",
        "AGENT_VOICE_API_KEY",
        "AGENT_VOICE_DEVICE",
        "AGENT_VOICE_DEVICES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("DEVICE", "cuda")
    monkeypatch.setenv("API_KEY", "leaked-key")

    settings = Settings(_env_file=None)

    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.device == "auto"
    assert settings.devices == {}
    assert settings.api_key == ""
    assert settings.home == default_home() == REPO_ROOT


def test_a_bad_value_in_the_environment_is_still_a_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix must not swallow real `AGENT_VOICE_*` validation."""
    monkeypatch.setenv("AGENT_VOICE_PORT", "not-a-port")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert "AGENT_VOICE_PORT" in str(excinfo.value)


# ---------------------------------------------------------------------------
# mcp-config: the absolute home, from anywhere
# ---------------------------------------------------------------------------


def _mcp_args(payload: dict) -> list[str]:
    server = payload["mcpServers"]["agent-voice"]
    return list(server["args"])


def test_mcp_config_names_the_resolved_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENT_VOICE_HOME", str(home))
    reset_settings_cache()

    result = runner.invoke(app, ["mcp-config", "--client", "claude-code"])

    assert result.exit_code == 0, result.output
    args = _mcp_args(json.loads(result.output))
    assert args == ["run", "--directory", str(home.resolve()), "agent-voice", "mcp"]


def test_mcp_config_from_another_cwd_names_the_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AGENT_VOICE_HOME", raising=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = runner.invoke(app, ["mcp-config", "--client", "claude-code"])

    assert result.exit_code == 0, result.output
    args = _mcp_args(json.loads(result.output))
    # Absolute, and the checkout — not site-packages, not the CWD.
    assert args[2] == str(REPO_ROOT)
    assert Path(args[2]).is_absolute()
    assert list(elsewhere.iterdir()) == []


# ---------------------------------------------------------------------------
# `engines list` is CWD-independent
# ---------------------------------------------------------------------------


@pytest.fixture
def tiny_engines_dir(tmp_path: Path) -> Path:
    """A two-engine copy of the shipped dependency-free engines.

    No `.venv`, no torch probe, no weights: the list is real but the fixture is
    fast. Enough to prove the output does not depend on the CWD.
    """
    target = tmp_path / "engines"
    for engine_id in ("mock", "mock_asr"):
        shutil.copytree(
            REPO_ENGINES / engine_id,
            target / engine_id,
            ignore=shutil.ignore_patterns(".venv", "__pycache__"),
        )
    return target


def test_engines_list_matches_from_an_empty_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tiny_engines_dir: Path
) -> None:
    empty = tmp_path / "empty-cwd"
    empty.mkdir()
    monkeypatch.setenv("AGENT_VOICE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ENGINES_DIR", str(tiny_engines_dir))

    monkeypatch.chdir(REPO_ROOT)
    reset_settings_cache()
    from_repo = _engines_list()

    monkeypatch.chdir(empty)
    reset_settings_cache()
    from_anywhere = _engines_list()

    assert from_anywhere == from_repo
    assert [entry["engine_id"] for entry in from_anywhere] == ["mock", "mock_asr"]
    # The whole point: no `data/`, `engines/` or `.env` appeared under the CWD.
    assert list(empty.iterdir()) == []


def test_engines_list_matches_the_repo_root_from_an_empty_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The v0.6.0 acceptance gate, on the real checkout.

    `ENGINES_DIR` is deliberately unset so the answer comes from `default_home()`,
    which is exactly what a fresh `agent-voice engines list` in an unrelated
    directory has to get right.
    """
    empty = tmp_path / "empty-cwd"
    empty.mkdir()
    monkeypatch.delenv("AGENT_VOICE_HOME", raising=False)
    monkeypatch.delenv("ENGINES_DIR", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    monkeypatch.chdir(REPO_ROOT)
    reset_settings_cache()
    from_repo = _engines_list()

    monkeypatch.chdir(empty)
    reset_settings_cache()
    from_anywhere = _engines_list()

    assert from_anywhere == from_repo
    ids = [entry["engine_id"] for entry in from_anywhere]
    assert "mock" in ids
    assert list(empty.iterdir()) == []
