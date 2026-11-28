"""Transport-neutral history recording."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..schemas import (
    AgentVoiceError,
    DesignVoiceResult,
    ErrorResponse,
    HistoryItem,
    ScriptResult,
    SpeakRequest,
    SynthesisResult,
    TranscribeResult,
)


def _voice_name(ctx: Any, voice_id: str | None) -> str:
    if not voice_id:
        return ""
    try:
        return ctx.voices.get(voice_id).name
    except AgentVoiceError:
        return ""


def _device(ctx: Any, engine_id: str) -> str | None:
    try:
        return ctx.workers.worker_devices().get(engine_id)
    except Exception:
        return None


def _publish(ctx: Any, item: HistoryItem) -> HistoryItem:
    try:
        ctx.events.publish("history", {"item": item.model_dump(mode="json")})
    except Exception:
        pass
    return item


def record_speak(
    ctx: Any,
    *,
    result: SynthesisResult,
    text: str,
    request: SpeakRequest | None = None,
) -> HistoryItem:
    item = ctx.history.record(
        kind="speak",
        text=text,
        voice_id=result.voice_id,
        voice_name=_voice_name(ctx, result.voice_id),
        speaker=result.speaker,
        engine_id=result.engine_id,
        model_alias=result.model_alias,
        device=_device(ctx, result.engine_id),
        seed=request.seed if request else None,
        take=result.take,
        duration_sec=result.duration_sec,
        format=result.format,
        artifact_id=result.artifact_id,
        audio_url=result.download_url or f"/audio/{result.artifact_id}",
        rerenderable=bool(request),
        request=request.model_dump(mode="json") if request else {},
    )
    return _publish(ctx, item)


def record_script(
    ctx: Any,
    *,
    result: ScriptResult,
    beats: list[dict[str, Any]],
    request: Any = None,
) -> HistoryItem:
    text = " · ".join(str(beat.get("text", "")) for beat in beats[:2])
    item = ctx.history.record(
        kind="script",
        text=text,
        engine_id=result.engine_id,
        model_alias=result.model_alias,
        device=_device(ctx, result.engine_id),
        duration_sec=result.duration_sec,
        format=result.format,
        artifact_id=result.artifact_id,
        audio_url=f"/audio/{result.artifact_id}",
        rerenderable=False,
        request=request.model_dump(mode="json") if hasattr(request, "model_dump") else {},
    )
    return _publish(ctx, item)


def record_design(ctx: Any, *, result: DesignVoiceResult, request: Any = None) -> HistoryItem:
    artifact_id = ""
    for candidate in result.candidates:
        url = candidate.download_url.rstrip("/")
        if url:
            artifact_id = url.rsplit("/", 1)[-1]
            break
    if not artifact_id:
        stem = Path(result.preview_file_path).stem
        artifact_id = stem if len(stem) == 16 else ""
    item = ctx.history.record(
        kind="design",
        text=result.name,
        voice_id=result.voice_id,
        voice_name=result.name,
        engine_id=result.engine_id,
        model_alias=result.model_alias,
        device=_device(ctx, result.engine_id),
        duration_sec=result.duration_sec,
        format="wav",
        artifact_id=artifact_id or None,
        audio_url=f"/audio/{artifact_id}" if artifact_id else "",
        rerenderable=request is not None,
        request=request.model_dump(mode="json") if hasattr(request, "model_dump") else {},
    )
    return _publish(ctx, item)


def record_transcribe(ctx: Any, *, result: TranscribeResult) -> HistoryItem:
    item = ctx.history.record(
        kind="transcribe",
        text=result.text,
        engine_id=result.engine_id,
        model_alias=result.model_alias,
        duration_sec=result.duration_sec,
        format="json",
        capture_id=result.capture_id,
        audio_url=f"/v1/captures/{result.capture_id}/audio" if result.capture_id else "",
        rerenderable=False,
        request={},
    )
    return _publish(ctx, item)


def record_failure(
    ctx: Any,
    *,
    kind: str,
    error: AgentVoiceError,
    request: dict[str, Any] | None = None,
    text: str = "",
) -> HistoryItem:
    item = ctx.history.record(
        kind=kind,  # type: ignore[arg-type]
        status="failed",
        text=text,
        error=ErrorResponse(
            error_code=error.error_code,
            message=error.message,
            suggestion=error.suggestion,
        ),
        request=request or {},
    )
    return _publish(ctx, item)
