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
from ..schemas import (
    AgentVoiceError,
    DesignCandidate,
    DesignVoiceRequest,
    DesignVoiceResult,
    ErrorCode,
    SynthesisResult,
)


async def render_design(
    ctx,
    payload: DesignVoiceRequest,
    *,
    agent_id: str | None = None,
) -> DesignVoiceResult:
    """Render ``payload.instruct`` as a voice and optionally register it.

    ``candidates > 1`` auditions several takes of the same description instead of
    saving one: each take is rendered with its own derived seed and reported back,
    so the caller can pick a voice before committing to it. ``take``/``seed`` are
    ignored in that mode — the takes are numbered 1..candidates and every seed is
    derived from its take.

    Args:
        ctx: Application context (settings, registry, workers, voices).
        payload: The description, the anchor sentence to speak, and where to save it.
        agent_id: Selective-routing id from ``X-Agent-Id``, if the transport has one.
            Not used for voice/engine selection: a design render is its own
            reference, so inheriting a binding would defeat the purpose.

    Returns:
        The rendered preview plus, when ``save_as_clone``, the new voice id. With
        ``candidates > 1`` nothing is saved and every candidate take is listed.
    """
    sample_limit = ctx.settings.max_design_sample_chars
    if len(payload.sample_text) > sample_limit:
        # Rejected before resolve_target: an over-long anchor is a caller mistake,
        # and waking the GPU to discover it would be pure waste.
        raise AgentVoiceError(
            ErrorCode.TEXT_TOO_LONG,
            f"sample_text is {len(payload.sample_text)} characters, limit is "
            f"{sample_limit}",
            "Shorten sample_text: one or two sentences (5-8 s of speech) is enough "
            "to audition a design.",
        )

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

    async def render_take(take: int | None, seed: int | None) -> SynthesisResult:
        """One design render; candidate takes differ only in the seed they get."""
        # No reference voice, no transcript, no prompt cache: the instruct *is* the
        # voice, so a cloned prompt would override the very thing being designed.
        return await ctx.workers.synthesize(
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
            seed=seed,
            response_format="wav",
            take=take,
        )

    if payload.candidates > 1:
        # Auditioning and saving are mutually exclusive: there is no single render
        # to register until the caller has chosen one, so save_as_clone is moot.
        #
        # seed=None lets WorkerManager.effective_seed() derive 10007 * take, so
        # each candidate is a genuinely different delivery that is still
        # reproducible — and the derived seed is reported, so the winner can be
        # re-rendered on its own later.
        candidates: list[DesignCandidate] = []
        for take in range(1, payload.candidates + 1):
            rendered = await render_take(take, None)
            candidates.append(
                DesignCandidate(
                    take=take,
                    seed=ctx.workers.effective_seed(None, take),
                    file_path=rendered.file_path,
                    download_url=rendered.download_url,
                    duration_sec=rendered.duration_sec,
                )
            )
        return DesignVoiceResult(
            voice_id=None,
            name=payload.name,
            # The first take doubles as the preview, so a caller that ignores the
            # candidate list still has something to play.
            preview_file_path=candidates[0].file_path,
            duration_sec=candidates[0].duration_sec,
            design_instruct=payload.instruct,
            engine_id=target.engine_id,
            model_alias=target.model_alias,
            candidates=candidates,
        )

    result = await render_take(payload.take, payload.seed)

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
