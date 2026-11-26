"""v0.3 voice design, end to end on the mock engine: describe, save, reuse.

The mock engine declares all three checkpoint modes — ``mock-tone`` (clone),
``mock-custom`` (custom_voice, presets Ryan/Serena) and ``mock-design``
(voice_design) — so the whole "design once, speak a thousand times" path runs
in-process with no GPU, no torch and no downloads.

Every test drives the real service layer against ``AppContext(Settings(...))``,
which is the same wiring the daemon, the CLI and the Manim demo use. The autouse
``isolated_env`` fixture in conftest.py gives each test a private data dir.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_voice.api.app import AppContext
from agent_voice.api.routes_openai import run_synthesis
from agent_voice.config import Settings
from agent_voice.schemas import (
    AgentVoiceError,
    BeatSpec,
    DesignVoiceRequest,
    ErrorCode,
    ScriptRequest,
    SynthesisResult,
)
from agent_voice.services.script_service import render_script
from agent_voice.services.voice_design_service import render_design

REPO_ROOT = Path(__file__).resolve().parents[1]

DESIGN_INSTRUCT = "a warm, low, unhurried female narrator in her forties"
"""A description, not a clip: this is the whole input a design needs."""

OTHER_INSTRUCT = "a bright, quick, high-pitched young voice"
"""Deliberately different: the mock engine derives its pitch from the instruct,
so a second design of the same name renders genuinely different audio."""


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=REPO_ROOT / "engines",
        AGENT_VOICE_DEFAULT_ENGINE="mock",
        MCP_BASE_PATH="",
    )
    base.update(overrides)
    return Settings(**base)


@contextlib.asynccontextmanager
async def mock_context(tmp_path: Path, **overrides) -> AsyncIterator[AppContext]:
    """A started worker manager and a private data dir, torn down either way."""
    ctx = AppContext(_settings(tmp_path, **overrides))
    await ctx.workers.start()
    try:
        yield ctx
    finally:
        await ctx.workers.stop()


class _ContextRequest:
    """The two attributes ``run_synthesis`` reads: the app context and headers.

    ``run_synthesis`` is the real REST synthesis path (text validation, then
    ``resolve_target``, then the worker). Driving it through this stand-in
    exercises that code without an HTTP server or a second copy of the routing
    logic in the test.
    """

    def __init__(self, ctx: AppContext) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(ctx=ctx))
        self.headers: dict[str, str] = {}


async def _speak(ctx: AppContext, *, text: str, voice: str | None = None) -> SynthesisResult:
    return await run_synthesis(
        _ContextRequest(ctx),
        text=text,
        voice=voice,
        engine=None,
        model=None,
        quality="high",
        language="Auto",
        response_format="wav",
        seed=None,
        speed=1.0,
    )


# ---------------------------------------------------------------------------
# render_design: the service behind POST /v1/voices/design and design_voice()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_design_saves_a_reusable_voice(tmp_path):
    """A design render is registered as an ordinary voice, provenance included."""
    async with mock_context(tmp_path) as ctx:
        result = await render_design(
            ctx, DesignVoiceRequest(name="narrator", instruct=DESIGN_INSTRUCT)
        )

        assert result.source == "designed"
        assert result.name == "narrator"
        assert result.design_instruct == DESIGN_INSTRUCT
        assert result.engine_id == "mock"
        assert result.model_alias == "mock-design", "design must pick a design checkpoint"
        assert result.voice_id is not None, "save_as_clone defaults to true"
        preview = Path(result.preview_file_path)
        assert preview.is_file()
        assert result.duration_sec > 0

        saved = ctx.voices.get(result.voice_id)
        assert saved.source == "designed"
        assert saved.design_instruct == DESIGN_INSTRUCT
        assert saved.ref_text, "the anchor sentence becomes the reference transcript"
        assert saved.voice_id == result.voice_id


@pytest.mark.asyncio
async def test_render_design_without_saving_leaves_no_voice(tmp_path):
    """A preview-only design is a render and nothing else: no store mutation."""
    async with mock_context(tmp_path) as ctx:
        result = await render_design(
            ctx,
            DesignVoiceRequest(name="preview-only", instruct=DESIGN_INSTRUCT, save_as_clone=False),
        )

        assert result.voice_id is None
        assert Path(result.preview_file_path).is_file()
        assert ctx.voices.list() == []


@pytest.mark.asyncio
async def test_render_design_is_idempotent_for_the_same_name_and_seed(tmp_path):
    """A retried design must not pile up duplicate voices.

    Same description + same seed is the same render (the synthesis cache answers
    it) and therefore the same reference audio, so ``voices.add`` returns the
    existing voice instead of raising or creating a second one.
    """
    async with mock_context(tmp_path) as ctx:
        first = await render_design(
            ctx, DesignVoiceRequest(name="repeatable", instruct=DESIGN_INSTRUCT, seed=11)
        )
        second = await render_design(
            ctx, DesignVoiceRequest(name="repeatable", instruct=DESIGN_INSTRUCT, seed=11)
        )

        assert first.voice_id is not None
        assert second.voice_id == first.voice_id
        assert len(ctx.voices.list()) == 1
        # A repeatable design reuses the cached render rather than re-rendering.
        assert second.preview_file_path == first.preview_file_path


@pytest.mark.asyncio
async def test_render_design_needs_overwrite_to_replace_a_name(tmp_path):
    """Re-designing a name with a different description is a clash until asked."""
    async with mock_context(tmp_path) as ctx:
        first = await render_design(
            ctx, DesignVoiceRequest(name="mutable", instruct=DESIGN_INSTRUCT)
        )

        with pytest.raises(AgentVoiceError) as excinfo:
            await render_design(ctx, DesignVoiceRequest(name="mutable", instruct=OTHER_INSTRUCT))
        assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
        assert "overwrite" in excinfo.value.suggestion.lower()

        replacement = await render_design(
            ctx,
            DesignVoiceRequest(name="mutable", instruct=OTHER_INSTRUCT, overwrite=True),
        )
        assert replacement.voice_id is not None
        assert replacement.voice_id != first.voice_id
        # Replacing drops the old directory, so its prompt caches cannot resurface.
        assert ctx.voices.resolve(first.voice_id) is None
        remaining = ctx.voices.list()
        assert [voice.voice_id for voice in remaining] == [replacement.voice_id]
        assert remaining[0].design_instruct == OTHER_INSTRUCT


# ---------------------------------------------------------------------------
# Mixed-mode scripts: one render, three ways of choosing a timbre
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_speak_script_records_mode_and_speaker_per_beat(tmp_path, ref_wav):
    """A podcast can clone one beat, preset the next and design the third.

    Each ``BeatTiming`` has to say which of the three actually spoke it, or a
    multi-speaker script is not auditable from its own result.
    """
    async with mock_context(tmp_path, MAX_RESIDENT_MODELS=3) as ctx:
        voice = ctx.voices.add(
            name="clone-beat", source_path=ref_wav, ref_text="a reference transcript"
        )
        beats = [
            BeatSpec(text="Cloned line.", voice=voice.voice_id),
            BeatSpec(text="Preset line.", speaker="Serena"),
            BeatSpec(text="Designed line.", instruct="a gravelly old storyteller"),
        ]

        result = await render_script(ctx, ScriptRequest(beats=beats))

        assert [beat.mode for beat in result.beats] == ["clone", "custom_voice", "voice_design"]
        assert result.beats[0].voice_id == voice.voice_id
        assert result.beats[0].speaker is None
        assert result.beats[0].instruct == "", "clone has no instruct channel"
        assert result.beats[1].voice_id is None, "a preset speaker has no reference clip"
        assert result.beats[1].speaker == "Serena"
        assert result.beats[2].voice_id is None
        assert result.beats[2].speaker is None
        assert result.beats[2].instruct == "a gravelly old storyteller"
        assert Path(result.file_path).is_file()
        assert result.duration_sec > 0


# ---------------------------------------------------------------------------
# Design, then speak: the bridge the whole feature exists for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_designed_voice_is_speakable_afterwards(tmp_path):
    """Once saved, a designed voice is just a reference clip — and a cheap one."""
    async with mock_context(tmp_path) as ctx:
        designed = await render_design(
            ctx, DesignVoiceRequest(name="narrator", instruct=DESIGN_INSTRUCT)
        )
        assert designed.voice_id is not None

        first = await _speak(ctx, text="The designed voice speaks.", voice=designed.voice_id)
        assert first.mode == "clone"
        assert first.voice_id == designed.voice_id
        assert first.model_alias == "mock-tone", "speaking reuses the clone checkpoint"
        assert first.cached is False
        assert Path(first.file_path).is_file()

        again = await _speak(ctx, text="The designed voice speaks.", voice=designed.voice_id)
        assert again.cached is True, "speaking a saved design twice costs no render"
        assert again.artifact_id == first.artifact_id
