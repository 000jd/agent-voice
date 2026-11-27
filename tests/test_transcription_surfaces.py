"""Transcription across every surface: service, REST, MCP and the CLI.

Everything runs on ``mock_asr`` and real ffprobe WAVs, so no GPU, no torch and
no model download is involved. The one CLI dubbing smoke test uses the ``mock``
TTS engine for the same reason.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agent_voice.api.app import AppContext, create_app
from agent_voice.api.routing import resolve_asr_target
from agent_voice.cli import app as cli_app
from agent_voice.config import Settings
from agent_voice.schemas import AgentVoiceError, ErrorCode, TranscribeResult
from agent_voice.services.transcribe_service import transcribe_media
from tests.test_mcp_proxy import mcp_client, payload

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RATE = 16_000


def _settings(tmp_path: Path, **overrides) -> Settings:
    """Engine manifests only; no venv is needed for the mock engines."""
    engines = tmp_path / "engines"
    for entry in (REPO_ROOT / "engines").iterdir():
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
        AGENT_VOICE_DEFAULT_ASR_ENGINE="mock_asr",
        MCP_BASE_PATH="",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def wav(tmp_path: Path) -> Path:
    """A real 3 s, 16 kHz mono WAV. The mock ASR reads only its duration."""
    time = np.linspace(0.0, 3.0, SAMPLE_RATE * 3, endpoint=False)
    tone = (0.2 * np.sin(2 * np.pi * 220.0 * time)).astype(np.float32)
    path = tmp_path / "clip.wav"
    sf.write(path, tone, SAMPLE_RATE, subtype="PCM_16")
    return path


@contextlib.asynccontextmanager
async def _started_ctx(settings: Settings) -> AsyncIterator[AppContext]:
    ctx = AppContext(settings)
    await ctx.workers.start()
    try:
        yield ctx
    finally:
        await ctx.workers.stop()


@pytest.fixture
async def app_ctx(settings: Settings) -> AsyncIterator[AppContext]:
    async with _started_ctx(settings) as ctx:
        yield ctx


async def _transcribe(ctx: AppContext, wav: Path, **overrides: object):
    target = resolve_asr_target(
        settings=ctx.settings, registry=ctx.registry, engine="mock_asr"
    )
    return await transcribe_media(ctx, audio_path=wav, target=target, **overrides)


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


async def test_service_writes_the_full_artifact_and_a_bounded_preview(
    app_ctx: AppContext, wav: Path
) -> None:
    result = await _transcribe(app_ctx, wav)

    assert result.text
    assert result.language == "English"
    assert result.segments_count >= 2
    assert len(result.segments) <= 40
    assert result.realtime_multiple is not None and result.realtime_multiple > 0
    assert result.transcript_json_path is not None

    artifact_path = Path(result.transcript_json_path)
    assert artifact_path.is_file()
    artifact = json.loads(artifact_path.read_text("utf-8"))
    assert len(artifact["segments"]) == result.segments_count
    assert artifact["source_path"] == str(wav)
    assert artifact["model_alias"] == "mock-asr-parity"
    assert artifact["segments"][0]["text"] == result.segments[0].text


async def test_word_timestamps_are_opt_in(app_ctx: AppContext, wav: Path) -> None:
    plain = await _transcribe(app_ctx, wav, word_timestamps=False)
    assert all(not segment.words for segment in plain.segments)

    detailed = await _transcribe(app_ctx, wav, word_timestamps=True)
    assert any(segment.words for segment in detailed.segments)
    for segment in detailed.segments:
        for word in segment.words:
            assert segment.start_sec <= word.start_sec <= word.end_sec <= segment.end_sec


async def test_srt_and_vtt_are_written_beside_the_json(
    app_ctx: AppContext, wav: Path
) -> None:
    result = await _transcribe(app_ctx, wav, export_srt=True)

    assert result.srt_path is not None and result.vtt_path is not None
    srt_text = Path(result.srt_path).read_text("utf-8")
    vtt_text = Path(result.vtt_path).read_text("utf-8")
    assert result.segments[0].text in srt_text
    assert vtt_text.startswith("WEBVTT")


async def test_duration_cap_refuses_before_a_worker_is_needed(
    tmp_path: Path, wav: Path
) -> None:
    settings = _settings(tmp_path, MAX_TRANSCRIBE_SECONDS="1")
    async with _started_ctx(settings) as ctx:
        target = resolve_asr_target(
            settings=ctx.settings, registry=ctx.registry, engine="mock_asr"
        )
        with pytest.raises(AgentVoiceError) as excinfo:
            await transcribe_media(ctx, audio_path=wav, target=target)

    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    assert "limit is 1" in excinfo.value.message


async def test_hotwords_reach_the_engine(app_ctx: AppContext, wav: Path) -> None:
    result = await _transcribe(app_ctx, wav, hotwords=["CUDA", "Manim"])

    assert "CUDA" in result.text
    assert "Manim" in result.text


async def test_already_16k_skips_the_scratch_extraction(
    app_ctx: AppContext, wav: Path
) -> None:
    result = await _transcribe(app_ctx, wav, already_16k=True)

    assert result.segments_count >= 2
    assert list(app_ctx.settings.artifacts_dir.glob(".asr-*")) == []


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as test_client:
        yield test_client


def test_agent_transcribe_route_returns_the_canonical_result(
    client: TestClient, wav: Path
) -> None:
    response = client.post(
        "/v1/agent/transcribe",
        json={"audio_path": str(wav), "engine": "mock_asr"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"]
    assert body["segments_count"] >= 2
    assert Path(body["transcript_json_path"]).is_file()
    assert "realtime_multiple" in body


def test_openai_transcription_json_text_and_subtitles(
    client: TestClient, wav: Path
) -> None:
    upload = {"file": ("clip.wav", wav.read_bytes(), "audio/wav")}
    common = {"model": "mock-asr-parity"}

    as_json = client.post(
        "/v1/audio/transcriptions", data={**common, "response_format": "json"}, files=upload
    )
    assert as_json.status_code == 200, as_json.text
    body = as_json.json()
    assert body["text"]
    assert body["segments_count"] >= 2
    assert "segments" in body

    as_text = client.post(
        "/v1/audio/transcriptions", data={**common, "response_format": "text"}, files=upload
    )
    assert as_text.status_code == 200
    assert as_text.text.strip()

    as_srt = client.post(
        "/v1/audio/transcriptions", data={**common, "response_format": "srt"}, files=upload
    )
    assert as_srt.status_code == 200
    assert "-->" in as_srt.text

    verbose = client.post(
        "/v1/audio/transcriptions",
        data={**common, "response_format": "verbose_json"},
        files=upload,
    )
    assert verbose.status_code == 200
    assert verbose.json()["engine_id"] == "mock_asr"
    assert verbose.json()["segments"][0]["words"]


def test_transcribe_route_confines_paths(tmp_path: Path, wav: Path) -> None:
    settings = _settings(tmp_path, AGENT_VOICE_ALLOW_ANY_PATH="false")
    app = create_app(settings=settings, mount_mcp=False)

    outside = tmp_path / "elsewhere.wav"
    shutil.copy(wav, outside)

    with TestClient(app) as client:
        refused = client.post(
            "/v1/agent/transcribe",
            json={"audio_path": str(outside), "engine": "mock_asr"},
        )
        assert refused.status_code == 403, refused.text
        assert refused.json()["error_code"] == "PATH_OUTSIDE_BASE"

        settings.inbox_dir.mkdir(parents=True, exist_ok=True)
        inside = settings.inbox_dir / "clip.wav"
        shutil.copy(wav, inside)
        accepted = client.post(
            "/v1/agent/transcribe",
            json={"audio_path": str(inside), "engine": "mock_asr"},
        )

    assert accepted.status_code == 200, accepted.text


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_transcribe_returns_a_compact_result(tmp_path: Path, wav: Path) -> None:
    async with mcp_client(tmp_path) as (client, _app):
        result = payload(
            await client.call_tool(
                "transcribe", {"audio_path": str(wav), "engine": "mock_asr"}
            )
        )

    assert result["text"]
    assert result["segments_count"] >= 2
    assert Path(result["transcript_json_path"]).is_file()
    assert result["elapsed_ms"] >= 0
    assert "realtime_multiple" in result


@pytest.mark.asyncio
async def test_mcp_dub_video_prompt_is_registered(tmp_path: Path) -> None:
    async with mcp_client(tmp_path) as (client, _app):
        names = {prompt.name for prompt in await client.list_prompts()}

    assert {"narrate_video", "dub_video"} <= names


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_transcribe_end_to_end(wav: Path) -> None:
    result = CliRunner().invoke(
        cli_app, ["transcribe", str(wav), "--engine", "mock_asr", "--srt", "--words"]
    )

    assert result.exit_code == 0, result.output
    assert "transcript_json_path" in result.output
    assert "srt_path" in result.output
    assert "segments_count" in result.output


def test_cli_transcribe_forwards_project(
    wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    async def fake_transcribe(ctx, **kwargs):
        captured.update(kwargs)
        return TranscribeResult(
            text="ok",
            language="English",
            duration_sec=3.0,
            segments_count=0,
            engine_id="mock_asr",
            model_alias="mock-asr-parity",
        )

    import agent_voice.services.transcribe_service as service

    monkeypatch.setattr(service, "transcribe_media", fake_transcribe)

    result = CliRunner().invoke(
        cli_app,
        ["transcribe", str(wav), "--engine", "mock_asr", "--project", "demo"],
    )

    assert result.exit_code == 0, result.output
    assert captured["project"] == "demo"


def _first_json(text: str) -> dict:
    start = text.index("{")
    return json.JSONDecoder().raw_decode(text[start:])[0]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="dubbing needs ffmpeg")
def test_cli_dub_prepare_and_render_smoke(tmp_path: Path) -> None:
    video = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=160x120:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ],
        shell=False,
        check=True,
        capture_output=True,
    )

    prepared = CliRunner().invoke(
        cli_app,
        [
            "dub",
            "prepare",
            str(video),
            "--asr-engine",
            "mock_asr",
            "--no-speaker-ref",
        ],
    )
    assert prepared.exit_code == 0, prepared.output
    plan = _first_json(prepared.output)
    assert plan["segments"]

    segments_path = tmp_path / "segments.json"
    segments_path.write_text(
        json.dumps(
            [
                {
                    "index": segment["index"],
                    "start_sec": segment["start_sec"],
                    "end_sec": segment["end_sec"],
                    "text": f"Line {segment['index']}.",
                }
                for segment in plan["segments"]
            ]
        ),
        encoding="utf-8",
    )

    rendered = CliRunner().invoke(
        cli_app,
        [
            "dub",
            "render",
            plan["dub_id"],
            str(segments_path),
            "--engine",
            "mock",
            "--quality",
            "fast",
            "--no-bed",
        ],
    )

    assert rendered.exit_code == 0, rendered.output
    body = _first_json(rendered.output)
    assert Path(body["file_path"]).is_file()
    assert body["file_path"].endswith(f"dub_{plan['dub_id']}.mp4")
