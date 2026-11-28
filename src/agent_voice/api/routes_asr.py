"""Speech recognition and dubbing endpoints.

Three surfaces live here and they deliberately share one pipeline:

1. ``POST /v1/audio/transcriptions`` is OpenAI-compatible: a multipart upload, a
   ``response_format`` and nothing else. The bytes are staged into the inbox (a
   transcription reads a file, exactly like cloning does), then deleted: an
   upload is not a voice and must not accumulate.
2. ``POST /v1/agent/transcribe`` takes a confined local path and returns the
   canonical :class:`~agent_voice.schemas.TranscribeResult`.
3. ``/v1/agent/dub/*`` is the two-stage dub: prepare here (transcribe + extract a
   speaker reference), translate in the agent, render here.

The work itself lives in ``services.transcribe_service`` / ``services.dub_service``
so the MCP server performs the same steps with an explicit ``agent_id`` instead of
faking a Request — the same split ``routes_agent`` uses for scripts. This module is
only the HTTP transport: parse, confine, delegate.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from ..ingest import persist_upload
from ..schemas import (
    AgentVoiceError,
    DubPrepareRequest,
    DubRenderRequest,
    ErrorCode,
    ErrorResponse,
    JobStartedResponse,
    TranscribeRequest,
)
from ..security import resolve_media_path
from .routing import resolve_asr_target

router = APIRouter(tags=["asr"])

TRANSCRIPTION_FORMATS = ("json", "text", "srt", "vtt", "verbose_json")
"""What ``response_format`` accepts, in the OpenAI order plus ``verbose_json``."""

SUBTITLE_MEDIA_TYPES = {
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
}


def _media_path(ctx, raw: str, field: str) -> Path:
    """Resolve a caller-supplied media path under the one shared confinement policy.

    Delegates to :func:`agent_voice.security.resolve_media_path`, which is
    :func:`resolve_ref_audio_path` under a different ``field`` noun: a video that
    cannot be found must not be reported as a missing reference clip. Duplicating
    the three branches here is how REST and MCP would drift apart.
    """
    settings = ctx.settings
    return resolve_media_path(
        raw,
        inbox_dir=settings.inbox_dir,
        mcp_base_path=settings.mcp_base_path,
        allow_any_path=settings.allow_any_path,
        field=field,
    )


def _hotwords_from_prompt(prompt: str | None, cap: int) -> list[str] | None:
    """Turn an OpenAI ``prompt`` into decoder hotwords.

    The upstream Phonon server documents exactly this mapping: a comma-separated
    prompt is a term list, a plain sentence is split on whitespace. The result is
    capped rather than rejected, because a caller splitting a sentence will
    legitimately produce more pieces than the decoder's bias list accepts.

    Terms over 64 characters are dropped for the same reason: that is the bound
    ``validate_hotwords`` enforces, and a stray URL in a prose prompt must not
    turn a transcription into a rejected request.
    """
    if not prompt or not prompt.strip():
        return None
    parts = prompt.split(",") if "," in prompt else prompt.split()
    terms = [part.strip() for part in parts]
    usable = [term for term in terms if term and len(term) <= 64]
    return usable[:cap] or None




@router.post("/v1/audio/transcriptions")
async def transcriptions(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form("phonon-2.5"),
    language: str | None = Form(None),
    prompt: str | None = Form(None),
    response_format: str = Form("json"),
    temperature: float | None = Form(None),
):
    """OpenAI-compatible transcription: multipart upload in, text or subtitles out.

    ``json`` is the OpenAI shape (``text``, ``language``, ``duration``,
    ``segments``, ``segments_count``, ``transcript_json_path``); ``verbose_json``
    is the full :class:`~agent_voice.schemas.TranscribeResult` including engine,
    model and timing metadata. ``temperature`` is accepted and ignored: sampling
    is not exposed by the ASR engines, and rejecting it would break real clients.
    """
    from ..services.transcribe_service import transcribe_media

    if response_format not in TRANSCRIPTION_FORMATS:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"unsupported response_format: {response_format!r}",
            f"Use one of {', '.join(TRANSCRIPTION_FORMATS)}.",
        )

    ctx = request.app.state.ctx
    stages_subtitles = response_format in SUBTITLE_MEDIA_TYPES
    stage = Path(tempfile.mkdtemp(dir=str(ctx.settings.data_dir), prefix=".upload-"))
    destination = stage / "upload.bin"
    try:
        await persist_upload(file, destination, max_bytes=ctx.settings.ingest_max_bytes)
        target = resolve_asr_target(
            settings=ctx.settings,
            registry=ctx.registry,
            model=model,
        )
        result = await transcribe_media(
            ctx,
            audio_path=destination,
            target=target,
            language=language,
            # Per-word timings are extra decoder work, so only the caller who
            # asked for the verbose shape pays for them: that is the format with
            # somewhere to put them, and every other shape stays on the cheap path.
            word_timestamps=response_format == "verbose_json",
            hotwords=_hotwords_from_prompt(prompt, ctx.settings.max_hotwords),
            project=None,
            export_srt=stages_subtitles,
            agent_id=request.headers.get("x-agent-id"),
            capture=True,
            capture_source="upload",
            capture_title="Uploaded audio",
        )
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    from ..services.history_service import record_transcribe

    record_transcribe(ctx, result=result)

    if response_format == "text":
        return PlainTextResponse(result.text)
    if stages_subtitles:
        subtitle = result.srt_path if response_format == "srt" else result.vtt_path
        if not subtitle:
            raise AgentVoiceError(
                ErrorCode.INTERNAL_ERROR,
                f"the engine transcribed {response_format} but produced no subtitle file",
                "Re-run with response_format='json' and write the subtitles yourself "
                "from segments[].",
            )
        return FileResponse(subtitle, media_type=SUBTITLE_MEDIA_TYPES[response_format])
    if response_format == "verbose_json":
        return result.model_dump(mode="json")
    return {
        "text": result.text,
        "language": result.language,
        "duration": result.duration_sec,
        "segments": [segment.model_dump(mode="json") for segment in result.segments],
        "segments_count": result.segments_count,
        "transcript_json_path": result.transcript_json_path,
        "capture_id": result.capture_id,
    }


@router.post("/v1/agent/transcribe")
async def agent_transcribe(payload: TranscribeRequest, request: Request):
    """Transcribe a confined local audio/video path into timed segments.

    Unlike the OpenAI surface this returns the canonical result, including the
    path to the full transcript when the segments inline were truncated.
    """
    from ..services.transcribe_service import transcribe_media

    ctx = request.app.state.ctx
    audio_path = _media_path(ctx, payload.audio_path, "audio_path")
    target = resolve_asr_target(
        settings=ctx.settings,
        registry=ctx.registry,
        engine=payload.engine,
        model=payload.model,
    )
    result = await transcribe_media(
        ctx,
        audio_path=audio_path,
        target=target,
        language=payload.language,
        word_timestamps=payload.word_timestamps,
        hotwords=payload.hotwords,
        project=payload.project,
        export_srt=payload.export_srt,
        agent_id=request.headers.get("x-agent-id"),
        capture=True,
        capture_source="path",
        capture_title=audio_path.stem[:100],
    )
    from ..services.history_service import record_transcribe

    record_transcribe(ctx, result=result)
    return result.model_dump(mode="json")


@router.post("/v1/agent/dub/prepare")
async def agent_dub_prepare(payload: DubPrepareRequest, request: Request):
    """Stage a video for dubbing: transcribe it and extract a speaker reference.

    Deliberately not gated by ``require_agent_mutation``. Dubbing reads a confined
    media path and writes artifacts, exactly like ``speak``: it installs nothing
    and executes no new code, so the mutation opt-in does not apply.
    """
    from ..services.dub_service import prepare_dub

    ctx = request.app.state.ctx
    # A path escape is refused here, before any ffmpeg probe or worker wake. The
    # service confines the same path under the same policy; this only refuses it
    # earlier, so the two can never disagree about what is allowed.
    _media_path(ctx, payload.media_path, "media_path")
    return (await prepare_dub(ctx, payload)).model_dump(mode="json")


@router.post("/v1/agent/dub/render")
async def agent_dub_render(payload: DubRenderRequest, request: Request):
    """Synthesize translated lines onto the timeline and mux the dubbed video.

    Synchronous: a long dub should be started through ``POST /v1/agent/dub/jobs``
    so it can be polled. ``overflow_warnings`` names every line that still overran
    its slot at ``max_atempo`` — shorten those and render again.
    """
    from ..services.dub_service import render_dub

    ctx = request.app.state.ctx
    return (await render_dub(ctx, payload)).model_dump(mode="json")


@router.post("/v1/agent/dub/jobs")
async def start_dub_job(payload: DubRenderRequest, request: Request):
    """Start a dub render as a background job; poll ``GET /v1/jobs/{job_id}``.

    Same runner shape as the script job: progress is reported per translated line,
    so a long mux is observable instead of appearing stuck until it lands.
    """
    from ..services.dub_service import render_dub

    ctx = request.app.state.ctx
    job = ctx.jobs.create("dub")

    def on_progress(done: int, total: int, message: str) -> None:
        # Per-line progress, so a long dub is observable while it runs instead of
        # appearing stuck at 0.01 until the mux lands.
        ctx.jobs.update(job.job_id, progress=(done / total if total else 1.0), message=message)

    async def runner() -> None:
        ctx.jobs.mark_running(job.job_id)
        try:
            result = await render_dub(ctx, payload, on_progress=on_progress)
            ctx.jobs.succeeded(job.job_id, result.model_dump(mode="json"))
        except AgentVoiceError as exc:
            ctx.jobs.failed(job.job_id, exc.to_response())
        except asyncio.CancelledError:
            ctx.jobs.update(job.job_id, status="cancelled")
            raise
        except Exception as exc:
            ctx.jobs.failed(
                job.job_id,
                ErrorResponse(
                    error_code=ErrorCode.INTERNAL_ERROR,
                    message=str(exc),
                    suggestion="Check the daemon logs for a traceback.",
                ),
            )

    ctx.jobs.register_task(job.job_id, asyncio.create_task(runner()))
    return JobStartedResponse(job_id=job.job_id, kind="dub").model_dump(mode="json")
