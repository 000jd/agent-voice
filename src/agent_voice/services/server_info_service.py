"""Read-only server information shared by REST and MCP."""

from __future__ import annotations

from typing import Any

from .. import __version__
from ..schemas import PROTOCOL_VERSION, ServerInfoResponse


async def server_info(ctx: Any) -> ServerInfoResponse:
    entries = ctx.registry.list_entries()
    try:
        gpu = await ctx.workers.gpu_status()
    except Exception:
        gpu = {}
    voices = ctx.voices.list()
    web = (ctx.settings.web_dir / "200.html").is_file()
    next_steps: list[str] = []
    if not voices:
        next_steps.append("Create a voice from a clean sample in Voices.")
    installed_asr = any(entry.kind == "asr" and entry.installed for entry in entries)
    if not installed_asr:
        next_steps.append(
            f"Install an ASR engine ({ctx.settings.default_asr_engine}) to transcribe."
        )
    if not ctx.settings.allow_agent_engine_mutation:
        next_steps.append(
            "Model install/download is disabled until ALLOW_AGENT_ENGINE_MUTATION=true."
        )
    return ServerInfoResponse(
        version=__version__,
        protocol_version=PROTOCOL_VERSION,
        toolset=ctx.settings.toolset,
        ui_enabled=ctx.settings.ui_enabled,
        web_available=web,
        home=str(ctx.settings.home),
        data_dir=str(ctx.settings.data_dir),
        inbox_dir=str(ctx.settings.inbox_dir),
        artifacts_dir=str(ctx.settings.artifacts_dir),
        engines_dir=str(ctx.settings.engines_dir),
        default_engine=ctx.settings.default_engine,
        default_asr_engine=ctx.settings.default_asr_engine,
        default_voice_id=ctx.settings.default_voice_id,
        voices_count=len(voices),
        history_count=ctx.history.count(),
        captures_count=ctx.captures.count(),
        limits={
            "max_text_chars": ctx.settings.max_text_chars,
            "max_script_beats": ctx.settings.max_script_beats,
            "max_audio_bytes": ctx.settings.ingest_max_bytes,
            "max_audio_seconds": ctx.settings.voice_draft_max_duration_seconds,
            "max_drafts": ctx.settings.voice_draft_max_count,
            "max_history_items": ctx.settings.history_max_items,
            "max_capture_items": ctx.settings.capture_max_items,
            "max_hotwords": ctx.settings.max_hotwords,
        },
        engines=[entry.model_dump(mode="json") for entry in entries],
        gpu=gpu,
        next_steps=next_steps,
    )
