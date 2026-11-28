"""The daemon's /ui static contract."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_voice.api.app import create_app
from agent_voice.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def _app(tmp_path: Path, **overrides):
    settings = Settings(
        AGENT_VOICE_HOME=tmp_path,
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=REPO_ROOT / "engines",
        AGENT_VOICE_DEFAULT_ENGINE="mock",
        AGENT_VOICE_DEFAULT_ASR_ENGINE="mock_asr",
        AGENT_VOICE_ALLOWED_HOSTS="testserver",
        **overrides,
    )
    return create_app(settings=settings, mount_mcp=False)


def test_ui_redirects_deep_links_and_missing_assets(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        redirect = client.get("/ui", follow_redirects=False)
        assert redirect.status_code == 307
        assert redirect.headers["location"] == "/ui/"

        index = client.get("/ui/")
        assert index.status_code == 200
        assert "text/html" in index.headers["content-type"]
        assert "default-src 'self'" in index.headers["content-security-policy"]

        deep = client.get("/ui/voices")
        assert deep.status_code == 200
        assert deep.text == index.text

        missing = client.get("/ui/_app/immutable/does-not-exist.js")
        assert missing.status_code == 404
        assert "<html" not in missing.text.lower()


def test_ui_off_serves_nothing(tmp_path):
    with TestClient(_app(tmp_path, AGENT_VOICE_UI="off")) as client:
        assert client.get("/ui").status_code == 404
        assert client.get("/ui/").status_code == 404


def test_api_still_guarded_when_ui_is_public(tmp_path):
    with TestClient(_app(tmp_path, AGENT_VOICE_API_KEY="secret")) as client:
        assert client.get("/ui/").status_code == 200
        assert client.get("/v1/voices").status_code == 401
