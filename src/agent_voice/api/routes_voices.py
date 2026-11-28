"""Voice management, presets and previews."""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ..ingest import safe_zip_extract
from ..schemas import (
    AgentVoiceError,
    CloneVoiceRequest,
    DesignVoiceRequest,
    ErrorCode,
    PresetListResponse,
    PresetPreviewRequest,
    PresetVoice,
    PreviewRequest,
    VoiceDetail,
    VoiceListResponse,
    VoiceMetadata,
    VoicePatchRequest,
    VoicePreviewResult,
    VoiceSampleRequest,
)
from ..security import resolve_ref_audio_path
from ..services.voice_design_service import render_design
from ..services.voice_service import commit_prepared, detail, prepare_from_draft, summary

router = APIRouter(tags=["voices"])

PREVIEW_TEXT_LIMIT = 200


def _confined_path(request: Request, raw_path: str) -> Path:
    settings = request.app.state.ctx.settings
    return resolve_ref_audio_path(
        raw_path,
        mcp_base_path=settings.mcp_base_path,
        inbox_dir=settings.inbox_dir,
        allow_any_path=settings.allow_any_path,
    )


def _create_model(
    payload: CloneVoiceRequest,
    request: Request,
    *,
    overwrite: bool,
) -> VoiceMetadata:
    ctx = request.app.state.ctx
    if payload.draft_id is not None:
        prepared, cleanup = prepare_from_draft(
            ctx, draft_id=payload.draft_id, trim=payload.trim
        )
        try:
            return commit_prepared(
                ctx,
                name=payload.name,
                prepared_wav=prepared,
                ref_text=payload.ref_text,
                x_vector_only=payload.x_vector_only,
                language=payload.language,
                notes=payload.notes,
                consent=payload.consent,
                overwrite=overwrite,
            )
        finally:
            if cleanup is not None:
                shutil.rmtree(cleanup, ignore_errors=True)
    source = _confined_path(request, payload.ref_audio_path or "")
    return ctx.voices.add(
        name=payload.name,
        source_path=source,
        ref_text=payload.ref_text,
        x_vector_only=payload.x_vector_only,
        overwrite=overwrite,
        language=payload.language,
        notes=payload.notes,
        consent=payload.consent,
    )


async def create_voice_from_payload(
    payload: dict, request: Request, *, overwrite: bool = False
) -> dict:
    if "overwrite" in payload:
        overwrite = bool(payload["overwrite"])
    request_model = CloneVoiceRequest(**payload)
    metadata = _create_model(request_model, request, overwrite=overwrite)
    return metadata.model_dump(mode="json")


@router.post("/v1/voices")
async def create_voice(payload: CloneVoiceRequest, request: Request, overwrite: bool = False):
    metadata = _create_model(payload, request, overwrite=overwrite)
    return metadata.model_dump(mode="json")


@router.post("/v1/voices/design")
async def design_voice(payload: DesignVoiceRequest, request: Request):
    ctx = request.app.state.ctx
    result = await render_design(ctx, payload, agent_id=request.headers.get("x-agent-id"))
    from ..services.history_service import record_design

    record_design(ctx, result=result, request=payload)
    return result.model_dump(mode="json")


@router.get("/v1/voices", response_model=VoiceListResponse)
async def list_voices(request: Request):
    ctx = request.app.state.ctx
    return VoiceListResponse(voices=[summary(ctx, voice) for voice in ctx.voices.list()])


@router.get("/v1/voices/{voice_id}", response_model=VoiceDetail)
async def get_voice(voice_id: str, request: Request):
    ctx = request.app.state.ctx
    return detail(ctx, ctx.voices.get(voice_id))


@router.patch("/v1/voices/{voice_id}")
async def patch_voice(voice_id: str, payload: VoicePatchRequest, request: Request):
    ctx = request.app.state.ctx
    metadata = ctx.voices.update(
        voice_id, name=payload.name, notes=payload.notes, language=payload.language
    )
    return metadata.model_dump(mode="json")


@router.delete("/v1/voices/{voice_id}")
async def delete_voice(voice_id: str, request: Request):
    ctx = request.app.state.ctx
    if not ctx.voices.delete(voice_id):
        raise AgentVoiceError(
            ErrorCode.VOICE_NOT_FOUND,
            f"no such voice: {voice_id}",
            "Call list_voices() to see available ids.",
        )
    return {"deleted": voice_id}


@router.post("/v1/voices/{voice_id}/samples", status_code=201)
async def add_sample(voice_id: str, payload: VoiceSampleRequest, request: Request):
    ctx = request.app.state.ctx
    ctx.voices.get(voice_id)
    if payload.draft_id is not None:
        prepared, cleanup = prepare_from_draft(
            ctx, draft_id=payload.draft_id, trim=payload.trim
        )
        try:
            metadata = ctx.voices.add_sample(
                voice_id,
                prepared_wav=prepared,
                ref_text=payload.ref_text,
                make_primary=payload.make_primary,
            )
        finally:
            if cleanup is not None:
                shutil.rmtree(cleanup, ignore_errors=True)
        return metadata.model_dump(mode="json")
    source = _confined_path(request, payload.ref_audio_path or "")
    metadata = ctx.voices.add_sample(
        voice_id,
        prepared_wav=source,
        ref_text=payload.ref_text,
        make_primary=payload.make_primary,
    )
    return metadata.model_dump(mode="json")


@router.delete("/v1/voices/{voice_id}/samples/{sample_id}")
async def delete_sample(voice_id: str, sample_id: str, request: Request):
    ctx = request.app.state.ctx
    ctx.voices.remove_sample(voice_id, sample_id)
    return {"deleted": sample_id}


@router.get("/v1/presets", response_model=PresetListResponse)
async def list_presets(request: Request):
    ctx = request.app.state.ctx
    presets: list[PresetVoice] = []
    for entry in ctx.registry.list_entries():
        if entry.kind != "tts":
            continue
        for model in entry.models:
            if model.mode != "custom_voice":
                continue
            for speaker in model.preset_speakers:
                presets.append(
                    PresetVoice(
                        preset_id=f"{entry.engine_id}:{model.alias}:{speaker}",
                        engine_id=entry.engine_id,
                        model_alias=model.alias,
                        speaker=speaker,
                        display_name=speaker.replace("_", " "),
                        languages=entry.languages,
                    )
                )
    return PresetListResponse(presets=presets)


def _preview_text(text: str) -> str:
    text = text.strip()
    if not text:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            "preview text is empty",
            "Provide a short sentence to speak.",
        )
    if len(text) > PREVIEW_TEXT_LIMIT:
        raise AgentVoiceError(
            ErrorCode.TEXT_TOO_LONG,
            f"preview text is {len(text)} characters, limit is {PREVIEW_TEXT_LIMIT}",
            "Use a short sentence for a voice preview.",
        )
    return text


async def _preview(
    request: Request,
    *,
    text: str,
    voice: str | None,
    speaker: str | None,
    engine: str | None,
    model: str | None,
) -> VoicePreviewResult:
    from .routes_openai import run_synthesis

    result = await run_synthesis(
        request,
        text=_preview_text(text),
        voice=voice,
        engine=engine,
        model=model,
        quality="fast",
        language="Auto",
        response_format="wav",
        seed=None,
        speed=1.0,
        speaker=speaker,
        markup=False,
    )
    return VoicePreviewResult(
        artifact_id=result.artifact_id,
        download_url=result.download_url or f"/audio/{result.artifact_id}",
        duration_sec=result.duration_sec,
        engine_id=result.engine_id,
        model_alias=result.model_alias,
        voice_id=result.voice_id,
        speaker=result.speaker,
        cached=result.cached,
    )


@router.post("/v1/voices/{voice_id}/preview", response_model=VoicePreviewResult)
async def preview_voice(voice_id: str, payload: PreviewRequest, request: Request):
    ctx = request.app.state.ctx
    ctx.voices.get(voice_id)
    return await _preview(
        request,
        text=payload.text,
        voice=voice_id,
        speaker=None,
        engine=payload.engine,
        model=payload.model,
    )


@router.post("/v1/presets/preview", response_model=VoicePreviewResult)
async def preview_preset(payload: PresetPreviewRequest, request: Request):
    return await _preview(
        request,
        text=payload.text,
        voice=None,
        speaker=payload.speaker,
        engine=payload.engine,
        model=payload.model,
    )


@router.get("/v1/voices/{voice_id}/export")
async def export_voice(voice_id: str, request: Request):
    ctx = request.app.state.ctx
    metadata = ctx.voices.get(voice_id)
    temp = Path(tempfile.mkdtemp(dir=str(ctx.settings.data_dir), prefix=".voice-export-"))
    archive = temp / f"{voice_id}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        manifest = {
            "archive_version": 1,
            "voice": metadata.model_dump(mode="json"),
            "samples": [
                sample.model_dump(mode="json")
                for sample in ctx.voices.samples_for(metadata)
            ],
        }
        bundle.writestr("manifest.json", json.dumps(manifest, indent=2))
        bundle.write(ctx.voices.ref_wav_path(voice_id), "ref.wav")
        samples_dir = ctx.voices.voice_dir(voice_id) / "samples"
        if samples_dir.is_dir():
            for sample in sorted(samples_dir.glob("*.wav")):
                bundle.write(sample, f"samples/{sample.name}")
        lexicon = ctx.voices.lexicon_path(voice_id)
        if lexicon.is_file():
            bundle.write(lexicon, "pronunciations.json")

    def cleanup() -> None:
        shutil.rmtree(temp, ignore_errors=True)

    return FileResponse(
        archive,
        media_type="application/zip",
        filename=f"{metadata.name}.voice.zip",
        background=BackgroundTask(cleanup),
    )


@router.post("/v1/voices/import", status_code=201)
async def import_voice(
    request: Request,
    file: UploadFile = File(...),
    overwrite: bool = Query(False),
):
    ctx = request.app.state.ctx
    temp = Path(tempfile.mkdtemp(dir=str(ctx.settings.data_dir), prefix=".voice-import-"))
    try:
        archive = temp / "bundle.zip"
        with open(archive, "wb") as handle:
            while chunk := await file.read(1024 * 1024):
                handle.write(chunk)
        extracted = safe_zip_extract(archive, temp / "content")
        manifest_path = temp / "content" / "manifest.json"
        if not manifest_path.is_file():
            raise AgentVoiceError(
                ErrorCode.ARCHIVE_UNSAFE,
                "voice bundle has no manifest.json",
                "Export a voice from this daemon.",
            )
        manifest = json.loads(manifest_path.read_text("utf-8"))
        voice = manifest.get("voice") or {}
        ref = temp / "content" / "ref.wav"
        if not ref.is_file():
            raise AgentVoiceError(
                ErrorCode.ARCHIVE_UNSAFE,
                "voice bundle has no ref.wav",
                "Export a voice from this daemon.",
            )
        metadata = ctx.voices.add_prepared(
            name=str(voice.get("name") or "Imported voice"),
            prepared_wav=ref,
            ref_text=str(voice.get("ref_text") or ""),
            x_vector_only=bool(voice.get("x_vector_only_mode")),
            overwrite=overwrite,
            language=str(voice.get("language") or "Auto"),
            notes=str(voice.get("notes") or ""),
            consent=bool(voice.get("consent_recorded_at")),
        )
        for sample_path in sorted((temp / "content" / "samples").glob("*.wav")):
            if sample_path.name == "ref.wav":
                continue
            sample_id = sample_path.stem
            if sample_id == "primary":
                continue
            ctx.voices.add_sample(
                metadata.voice_id,
                prepared_wav=sample_path,
                ref_text=str(voice.get("ref_text") or ""),
            )
        lexicon = temp / "content" / "pronunciations.json"
        if lexicon.is_file():
            shutil.copyfile(lexicon, ctx.voices.lexicon_path(metadata.voice_id))
        if not extracted:
            raise AgentVoiceError(
                ErrorCode.ARCHIVE_UNSAFE,
                "voice bundle is empty",
                "Export a voice from this daemon.",
            )
        return metadata.model_dump(mode="json")
    except json.JSONDecodeError as exc:
        raise AgentVoiceError(
            ErrorCode.ARCHIVE_UNSAFE,
            "voice bundle manifest is not valid JSON",
            "Export a voice from this daemon.",
        ) from exc
    finally:
        shutil.rmtree(temp, ignore_errors=True)
