"""Voice design: turn a description into a reusable voice.

A design render is a synthesis followed, optionally, by a registration. Keeping
both steps here (rather than in the FastAPI route) means the MCP server performs
exactly the same work with an explicit caller identity instead of faking a
Request — the same reason ``script_service`` exists.

The design is persisted as an ordinary voice: once saved, it is just a reference
clip with a transcript, so every engine and every later call treats it exactly
like a clone. That is what makes "design once, speak a thousand times" cheap.
"""

from __future__ import annotations

from pathlib import Path

from ..api.routing import resolve_target
from ..schemas import DesignVoiceRequest, DesignVoiceResult


async def render_design(
    ctx,
    payload: DesignVoiceRequest,
    *,
    agent_id: str | None = None,
) -> DesignVoiceResult:
    """Render ``payload.instruct`` as a voice and optionally register it.

    Args:
        ctx: Application context (settings, registry, workers, voices).
        payload: The description, the anchor sentence to speak, and where to save it.
        agent_id: Selective-routing id from ``X-Agent-Id``, if the transport has one.
            Not used for voice/engine selection: a design render is its own
            reference, so inheriting a binding would defeat the purpose.

    Returns:
        The rendered preview plus, when ``save_as_clone``, the new voice id.
    """
    target = resolve_target(
        settings=ctx.settings,
        registry=ctx.registry,
        voices=ctx.voices,
        bindings=ctx.bindings,
        voice=None,
        speaker=None,
        instruct=payload.instruct,
        engine=payload.engine,
        model=payload.model,
        quality=payload.quality,
        force_mode="voice_design",
    )

    # No reference voice, no transcript, no prompt cache: the instruct *is* the
    # voice, so a cloned prompt would override the very thing being designed.
    result = await ctx.workers.synthesize(
        engine_id=target.engine_id,
        model_alias=target.model_alias,
        text=payload.sample_text,
        mode="voice_design",
        speaker=None,
        instruct=target.instruct,
        voice_id=None,
        ref_text="",
        x_vector_only=False,
        language=payload.language,
        seed=payload.seed,
        response_format="wav",
        take=payload.take,
    )

    voice_id: str | None = None
    if payload.save_as_clone:
        # Registering the render as a voice is what turns a one-off preview into a
        # reusable timbre; the instruct is kept as provenance for list_voices().
        metadata = ctx.voices.add(
            name=payload.name,
            source_path=Path(result.file_path),
            ref_text=payload.sample_text,
            source="designed",
            design_instruct=payload.instruct,
            overwrite=payload.overwrite,
        )
        voice_id = metadata.voice_id

    return DesignVoiceResult(
        voice_id=voice_id,
        name=payload.name,
        preview_file_path=result.file_path,
        duration_sec=result.duration_sec,
        design_instruct=payload.instruct,
        engine_id=target.engine_id,
        model_alias=target.model_alias,
    )
