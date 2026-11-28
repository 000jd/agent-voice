"""v0.7 ingest, draft, voice, history, capture and settings contracts."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from agent_voice.api.app import create_app
from agent_voice.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
SR = 24_000


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        AGENT_VOICE_HOME=tmp_path,
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=REPO_ROOT / "engines",
        AGENT_VOICE_DEFAULT_ENGINE="mock",
        AGENT_VOICE_DEFAULT_ASR_ENGINE="mock_asr",
        AGENT_VOICE_ALLOWED_HOSTS="testserver",
        AGENT_VOICE_ALLOW_ANY_PATH="true",
    )
    base.update(overrides)
    return Settings(**base)


def _wav_bytes(seconds: float = 3.0) -> bytes:
    t = np.linspace(0, seconds, int(SR * seconds), endpoint=False)
    data = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    buffer = io.BytesIO()
    sf.write(buffer, data, SR, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(settings=_settings(tmp_path), mount_mcp=False)
    with TestClient(app) as test_client:
        yield test_client


def _draft(client: TestClient, payload: bytes | None = None, name: str = "clip.wav"):
    return client.post(
        "/v1/voice-drafts",
        files={"file": (name, payload if payload is not None else _wav_bytes(), "audio/wav")},
    )


def test_playlist_is_rejected_before_ffmpeg(client):
    response = _draft(client, b"#EXTM3U\n#EXT-X-STREAM-INF:http://127.0.0.1:9/x\n", "evil.m3u8")
    assert response.status_code == 415
    assert response.json()["error_code"] == "UNSUPPORTED_MEDIA_TYPE"


def test_concat_script_is_rejected(client):
    response = _draft(client, b"ffconcat version 1.0\nfile '/etc/passwd'\n", "evil.ffconcat")
    assert response.status_code == 415


def test_png_renamed_as_wav_is_rejected(client):
    response = _draft(client, b"\x89PNG\r\n\x1a\nnot audio", "voice.wav")
    assert response.status_code == 415


def test_empty_upload_is_rejected(client):
    response = _draft(client, b"", "empty.wav")
    assert response.status_code == 415


def test_declared_oversize_is_rejected_early(client):
    response = client.post(
        "/v1/voice-drafts",
        content=b"x",
        headers={
            "Content-Type": "multipart/form-data; boundary=x",
            "Content-Length": str(600 * 1024**2),
        },
    )
    assert response.status_code == 413


def test_wav_draft_has_peaks_quality_and_transcript(client):
    response = _draft(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["draft_id"]
    assert body["quality"]["duration_sec"] == pytest.approx(3.0, abs=0.05)
    assert len(body["peaks"]) == 240
    assert max(body["peaks"]) <= 1.0
    assert body["suggested_transcript"] is not None


def test_trim_commit_preview_and_sample_promotion(client):
    draft = _draft(client).json()
    created = client.post(
        "/v1/voices",
        json={
            "name": "Studio test",
            "draft_id": draft["draft_id"],
            "trim": {"start_sec": 0.5, "end_sec": 2.5},
            "ref_text": "Studio test",
            "consent": True,
        },
    )
    assert created.status_code == 200, created.text
    voice = created.json()
    assert 1.9 <= voice["duration_sec"] <= 2.1
    detail = client.get(f"/v1/voices/{voice['voice_id']}")
    assert detail.status_code == 200
    assert detail.json()["voice"]["sample_count"] == 1

    second = _draft(client).json()
    added = client.post(
        f"/v1/voices/{voice['voice_id']}/samples",
        json={"draft_id": second["draft_id"], "ref_text": "second", "consent": True},
    )
    assert added.status_code == 201, added.text
    sample_id = added.json()["samples"][-1]["sample_id"]
    promoted = client.post(
        f"/v1/voices/{voice['voice_id']}/samples",
        json={"draft_id": second["draft_id"], "consent": True, "make_primary": True},
    )
    assert promoted.status_code == 201
    removed = client.delete(f"/v1/voices/{voice['voice_id']}/samples/{sample_id}")
    assert removed.status_code == 200
    assert client.get("/v1/voices").json()["voices"][0]["sample_count"] >= 1


def test_draft_commit_requires_consent(client):
    draft = _draft(client).json()
    response = client.post(
        "/v1/voices",
        json={"name": "No consent", "draft_id": draft["draft_id"]},
    )
    assert response.status_code == 422


def test_history_survives_app_restart(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as c:
        response = c.post("/v1/agent/speak", json={"text": "remember this", "speaker": "Ryan"})
        assert response.status_code == 200, response.text
        items = c.get("/v1/history").json()["items"]
        assert items and items[0]["kind"] == "speak"
        item_id = items[0]["id"]
        assert c.post(f"/v1/history/{item_id}/favorite").json()["favorite"] is True

    app2 = create_app(settings=settings, mount_mcp=False)
    with TestClient(app2) as c:
        items = c.get("/v1/history").json()["items"]
        assert any(item["id"] == item_id and item["favorite"] for item in items)


def test_capture_and_transcript_export(client):
    response = client.post(
        "/v1/audio/transcriptions",
        files={"file": ("clip.wav", _wav_bytes(), "audio/wav")},
        data={"model": "mock-asr-parity", "response_format": "verbose_json"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["capture_id"]
    capture_id = body["capture_id"]
    listed = client.get("/v1/captures")
    assert listed.status_code == 200
    assert any(item["id"] == capture_id for item in listed.json()["items"])
    srt = client.get(f"/v1/captures/{capture_id}/export", params={"format": "srt"})
    assert srt.status_code == 200
    assert "-->" in srt.text


def test_settings_write_is_atomic_and_redacted(tmp_path):
    (tmp_path / ".env").write_text("MAX_TEXT_CHARS=999\n", encoding="utf-8")
    settings = _settings(tmp_path)
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as c:
        response = c.patch("/v1/settings", json={"values": {"MAX_TEXT_CHARS": 12345}})
        assert response.status_code == 200, response.text
        assert response.json()["restart_required"] is True
        env_text = (tmp_path / ".env").read_text("utf-8")
        assert "MAX_TEXT_CHARS=12345" in env_text
        assert (tmp_path / ".env.bak").is_file()
        entries = c.get("/v1/settings").json()["settings"]
        api = next(entry for entry in entries if entry["key"] == "AGENT_VOICE_API_KEY")
        assert api["value"] is False


def test_forbidden_setting_is_refused(client):
    response = client.patch(
        "/v1/settings", json={"values": {"AGENT_VOICE_API_KEY": "new-secret"}}
    )
    assert response.status_code == 403
    assert response.json()["error_code"] == "FORBIDDEN_SETTING"


def test_packaged_ui_is_served(client):
    assert client.get("/v1/server_info").status_code == 200
    assert client.get("/ui/").status_code == 200


def test_new_routes_are_guarded(tmp_path):
    settings = _settings(tmp_path, AGENT_VOICE_API_KEY="secret")
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as c:
        for path in (
            "/v1/voice-drafts/0000000000000000",
            "/v1/history",
            "/v1/captures",
            "/v1/events",
            "/v1/settings",
            "/v1/server_info",
            "/v1/lexicon",
            "/v1/cache",
            "/v1/auth/check",
            "/v1/presets",
        ):
            response = c.get(path)
            assert response.status_code == 401, path
