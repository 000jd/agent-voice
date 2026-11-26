"""Shared pytest fixtures for the agent-voice contract suite.

These fixtures deliberately avoid torch and avoid spawning real GPU workers.
Engine-dependent tests use the ``mock`` engine.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_voice.clock import set_clock  # noqa: E402
from agent_voice.config import Settings, reset_settings_cache  # noqa: E402
from tests._fake_clock import FakeClock  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test gets a private data dir and a clean settings cache."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ENGINES_DIR", str(REPO_ROOT / "engines"))
    monkeypatch.setenv("AGENT_VOICE_DEFAULT_ENGINE", "mock")
    monkeypatch.setenv("MCP_BASE_PATH", "")
    monkeypatch.setenv("ALLOW_AGENT_ENGINE_MUTATION", "true")
    # The production default is now True (an agent-authored plugin is executable
    # code). Tests that exercise live registration opt out explicitly; staging
    # tests set it back to true.
    monkeypatch.setenv("REQUIRE_ENGINE_APPROVAL", "false")
    monkeypatch.setenv("MODEL_IDLE_TTL_SECONDS", "900")
    monkeypatch.setenv("MIN_ENGINE_RESIDENCY_SECONDS", "60")
    # Starlette's TestClient sends `Host: testserver`. That name is deliberately
    # absent from the production defaults, so the harness opts in exactly the
    # way an operator would: through the allowed-hosts setting.
    monkeypatch.setenv("AGENT_VOICE_ALLOWED_HOSTS", "testserver")
    monkeypatch.delenv("AGENT_VOICE_API_KEY", raising=False)
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def settings() -> Settings:
    from agent_voice.config import get_settings

    s = get_settings()
    s.ensure_dirs()
    return s


@pytest.fixture
def fake_clock() -> Iterator[FakeClock]:
    clock = FakeClock()
    set_clock(clock)
    yield clock


@pytest.fixture
def mock_engine_dir() -> Path:
    return REPO_ROOT / "engines" / "mock"


@pytest.fixture
def ref_wav(tmp_path: Path) -> Path:
    """A tiny real WAV so ffmpeg/ffprobe paths can be exercised without models."""
    import numpy as np
    import soundfile as sf

    sr = 24_000
    t = np.linspace(0.0, 3.0, sr * 3, endpoint=False)
    tone = (0.2 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    path = tmp_path / "ref.wav"
    sf.write(path, tone, sr, subtype="PCM_16")
    return path


@pytest.fixture
def has_ffmpeg() -> bool:
    from shutil import which

    return which("ffmpeg") is not None and which("ffprobe") is not None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip GPU tests unless the user explicitly asked for them."""
    if os.environ.get("AGENT_VOICE_RUN_GPU_TESTS") == "1":
        return
    skip_gpu = pytest.mark.skip(reason="set AGENT_VOICE_RUN_GPU_TESTS=1 to run GPU tests")
    for item in items:
        if "gpu" in item.keywords:
            item.add_marker(skip_gpu)
