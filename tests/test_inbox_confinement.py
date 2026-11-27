"""S2 regression: agent clone paths default to the data inbox.

`/etc/passwd` and `/proc/self/environ` must be rejected by the confinement
check before any ffmpeg probe, while a clip in ``<DATA_DIR>/inbox/`` works. The
operator can opt into arbitrary paths with AGENT_VOICE_ALLOW_ANY_PATH=true.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_voice.api.app import create_app
from agent_voice.config import Settings
from agent_voice.schemas import AgentVoiceError, ErrorCode
from agent_voice.security import resolve_media_path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path: Path, **overrides) -> Settings:
    engines = tmp_path / "engines"
    source = REPO_ROOT / "engines"
    for entry in source.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        target = engines / entry.name
        target.mkdir(parents=True)
        for filename in ("engine.yaml", "worker.py", "pyproject.toml"):
            candidate = entry / filename
            if candidate.is_file():
                (target / filename).write_bytes(candidate.read_bytes())

    base = dict(
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=engines,
        AGENT_VOICE_DEFAULT_ENGINE="mock",
        MCP_BASE_PATH="",
        AGENT_VOICE_ALLOW_ANY_PATH="false",
    )
    base.update(overrides)
    return Settings(**base)


def test_inbox_confinement_message_names_the_inbox(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(AgentVoiceError) as excinfo:
        resolve_media_path(
            "/etc/passwd",
            inbox_dir=settings.inbox_dir,
            mcp_base_path="",
            allow_any_path=False,
        )

    assert excinfo.value.error_code == ErrorCode.PATH_OUTSIDE_BASE
    assert "the inbox" in excinfo.value.message
    assert "MCP_BASE_PATH" not in excinfo.value.message


def test_mcp_base_confinement_message_keeps_the_historical_label(tmp_path):
    base = tmp_path / "sandbox"
    base.mkdir()
    with pytest.raises(AgentVoiceError) as excinfo:
        resolve_media_path(
            "/etc/passwd",
            inbox_dir=tmp_path / "inbox",
            mcp_base_path=str(base),
            allow_any_path=False,
        )

    assert excinfo.value.error_code == ErrorCode.PATH_OUTSIDE_BASE
    assert "MCP_BASE_PATH" in excinfo.value.message


@pytest.mark.parametrize("probe", ["/etc/passwd", "/proc/self/environ"])
def test_rest_clone_rejects_outside_paths_by_default(tmp_path, probe):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/voices",
            json={"name": "probe", "ref_audio_path": probe},
        )

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error_code"] == "PATH_OUTSIDE_BASE"
    assert "inbox" in body["suggestion"].lower()


def test_rest_clone_accepts_clip_in_inbox(tmp_path, ref_wav):
    settings = _settings(tmp_path)
    inbox = settings.inbox_dir
    inbox.mkdir(parents=True, exist_ok=True)
    target = inbox / "clip.wav"
    shutil.copy(ref_wav, target)

    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/voices",
            json={"name": "inboxvoice", "ref_audio_path": str(target)},
        )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "inboxvoice"


def test_rest_clone_allow_any_path_permits_outside_clip(tmp_path, ref_wav):
    settings = _settings(tmp_path, AGENT_VOICE_ALLOW_ANY_PATH="true")
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/voices",
            json={"name": "outsidevoice", "ref_audio_path": str(ref_wav)},
        )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "outsidevoice"


def test_rest_clone_allow_any_path_rejects_relative_path(tmp_path, ref_wav, monkeypatch):
    """L2: opting into arbitrary paths does not make the daemon's cwd a base.

    The clip below exists relative to cwd and is still refused, so the refusal is
    about the meaning of the path rather than about the file being absent.
    """
    monkeypatch.chdir(ref_wav.parent)
    settings = _settings(tmp_path, AGENT_VOICE_ALLOW_ANY_PATH="true")
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/voices",
            json={"name": "relvoice", "ref_audio_path": ref_wav.name},
        )

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error_code"] == "BAD_REQUEST"
    assert "relative ref_audio_path" in body["message"], body
    assert "absolute" in body["suggestion"].lower(), body


def test_resolver_is_the_single_policy_for_both_surfaces(tmp_path, ref_wav):
    """The three branches live in one function so REST and MCP cannot drift."""
    from agent_voice.schemas import AgentVoiceError
    from agent_voice.security import resolve_ref_audio_path

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    inside = inbox / "clip.wav"
    shutil.copy(ref_wav, inside)

    # MCP_BASE_PATH wins: a relative path is read inside the sandbox root.
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    shutil.copy(ref_wav, sandbox / "clip.wav")
    assert resolve_ref_audio_path(
        "clip.wav", mcp_base_path=str(sandbox), inbox_dir=inbox, allow_any_path=True
    ) == Path(sandbox / "clip.wav").resolve()

    # Default posture: the inbox only, and the refusal names it.
    with pytest.raises(AgentVoiceError) as inbox_error:
        resolve_ref_audio_path(str(ref_wav), inbox_dir=inbox, allow_any_path=False)
    assert inbox_error.value.error_code == "PATH_OUTSIDE_BASE"
    assert str(inbox) in inbox_error.value.suggestion

    # allow_any_path: any absolute existing file, never a relative one.
    assert resolve_ref_audio_path(
        str(ref_wav), inbox_dir=inbox, allow_any_path=True
    ) == Path(ref_wav)
    with pytest.raises(AgentVoiceError) as relative_error:
        resolve_ref_audio_path(ref_wav.name, inbox_dir=inbox, allow_any_path=True)
    assert relative_error.value.error_code == "BAD_REQUEST"
    assert "relative" in relative_error.value.message
