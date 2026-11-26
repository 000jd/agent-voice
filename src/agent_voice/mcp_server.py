"""MCP server: the agent-facing surface.

Every tool returns compact JSON. Audio is never inlined as base64 — the caller
gets a ``file_path`` it can read, materialised inside ``MCP_BASE_PATH/artifacts``
when that is configured so the path is reachable from a sandboxed agent.

``X-Agent-Id`` is read from the live HTTP request. Note that
``get_http_headers()`` withholds ``authorization`` by default, which is exactly
why Bearer auth is enforced by FastAPI middleware instead of in here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

from .schemas import (
    AgentVoiceError,
    BeatSpec,
    ErrorCode,
    ErrorResponse,
    ScriptRequest,
    SpeakRequest,
)

AGENT_ID_HEADER = "x-agent-id"


def _agent_id() -> str | None:
    headers = get_http_headers()
    return headers.get(AGENT_ID_HEADER)


def _error(exc: AgentVoiceError) -> dict[str, Any]:
    return exc.to_response().model_dump(mode="json")


def build_mcp_server(ctx) -> FastMCP:
    mcp: FastMCP = FastMCP(
        "agent-voice",
        instructions=(
            "Local voice cloning and speech synthesis. Call server_info() FIRST: "
            "it is free (no GPU wake) and tells you the version, whether any voice "
            "exists, which voices are registered and which engines are installed. "
            "Then call speak() for one utterance, speak_script() for multi-beat "
            "content with timings. Tools return file paths, never audio blobs. "
            "Use gpu_status() to see what is resident and unload_models() to free "
            "VRAM when another GPU application needs it."
        ),
    )

    # -- orientation ----------------------------------------------------
    @mcp.tool
    async def server_info() -> dict[str, Any]:
        """Call this first: a free, side-effect-free snapshot of the server.

        Never spawns a worker and never touches the GPU, so it costs nothing to
        call before you know what you are doing. Returns the version, the default
        engine and voice, every registered voice, every engine with its install
        state, and the same GPU snapshot as gpu_status().

        Read `voices_count` before synthesising: if it is 0 you must call
        clone_voice() first. Inspect `engines[].installed` /
        `why_unavailable` before choosing an engine, and `gpu_status` to see
        whether a model is already resident.

        Deliberately free of error handling that could change the shape: the
        registry and voice-store queries are pure reads of on-disk state, so the
        key set above is always exactly what the caller gets back.
        """
        from . import __version__

        entries = ctx.registry.list_entries(loaded=ctx.workers.loaded_models())
        voices = ctx.voices.list()
        default_voice = ctx.settings.default_voice_id or None
        if default_voice is None and len(voices) == 1:
            # A single registered voice is unambiguous: use it rather than
            # making the caller discover and pass its id.
            default_voice = voices[0].voice_id
        return {
            "version": __version__,
            "default_engine": ctx.settings.default_engine,
            "default_voice": default_voice,
            "voices_count": len(voices),
            "voices": [
                {
                    "voice_id": voice.voice_id,
                    "name": voice.name,
                    "duration_sec": voice.duration_sec,
                }
                for voice in voices
            ],
            "installed_engines": [e.engine_id for e in entries if e.installed],
            "engines": [
                {
                    "engine_id": e.engine_id,
                    "installed": e.installed,
                    "streaming": e.streaming,
                    "why_unavailable": e.why_unavailable,
                }
                for e in entries
            ],
            "gpu_status": await ctx.workers.gpu_status(),
        }

    # -- synthesis ------------------------------------------------------
    @mcp.tool
    async def speak(
        text: str,
        voice: str | None = None,
        engine: str | None = None,
        model: str | None = None,
        quality: str = "high",
        language: str = "Auto",
        format: str = "wav",
        seed: int | None = None,
        speed: float = 1.0,
    ) -> dict[str, Any]:
        """Synthesise one utterance and return a file path.

        Args:
            text: What to say.
            voice: Voice id or name. Defaults to the agent binding, then the server default.
            engine: Engine id, e.g. qwen3_tts_fast. Defaults to configured engine.
            model: Model alias, or `engine:model`. Overrides `quality`.
            quality: "high" (1.7B) or "fast" (0.6B).
            language: "Auto" or a language name such as "English".
            format: wav, mp3, opus, flac or pcm.
            seed: Fixed seed: the same inputs then produce byte-identical audio.
            speed: Playback-rate multiplier applied by the engine.

        Key return fields:
            file_path: Local absolute path you can read directly.
            download_url: HTTP URL for the same artifact, if you would rather hand
                a URL to another process.
            chunks_used: >1 means the text exceeded the chunk limit and was
                synthesised in pieces and stitched. That is normal, not an error.
            elapsed_ms: Wall-clock cost. On a cache hit this is near zero, which
                makes latency regressions obvious.
            cached: True when the deterministic disk cache answered the request.
        """
        from .api.routing import resolve_target

        try:
            request = SpeakRequest(
                text=text, voice=voice, engine=engine, model=model,
                quality=quality, language=language, format=format,
                seed=seed, speed=speed,
            )
            if len(request.text) > ctx.settings.max_text_chars:
                raise AgentVoiceError(
                    ErrorCode.TEXT_TOO_LONG,
                    f"text is {len(request.text)} characters, limit is "
                    f"{ctx.settings.max_text_chars}",
                    "Split it up or use speak_script.",
                )
            target = resolve_target(
                settings=ctx.settings,
                registry=ctx.registry,
                voices=ctx.voices,
                bindings=ctx.bindings,
                agent_id=_agent_id(),
                voice=request.voice,
                engine=request.engine,
                model=request.model,
                quality=request.quality,  # type: ignore[arg-type]
            )
            result = await ctx.workers.synthesize(
                engine_id=target.engine_id,
                model_alias=target.model_alias,
                text=request.text,
                voice_id=target.voice_id,
                ref_text=target.ref_text,
                x_vector_only=target.x_vector_only,
                language=request.language,
                seed=request.seed,
                speed=request.speed,
                response_format=request.format,
            )
            return result.model_dump(mode="json")
        except AgentVoiceError as exc:
            return _error(exc)

    @mcp.tool
    async def speak_script(
        beats: list[dict[str, Any]],
        default_voice: str | None = None,
        engine: str | None = None,
        quality: str = "high",
        format: str = "wav",
        export_srt: bool = True,
    ) -> dict[str, Any]:
        """Synthesise a multi-beat script with exact timings and optional subtitles.

        Each beat is an object with these keys (all optional except `text`):
        {text, voice?, engine?, model?, quality?, language?,
         target_duration_sec?, fit_mode?, pause_after_sec?}

        * `pause_after_sec` (0..10) inserts silence after the beat and is included
          in the reported timings, so subtitles and downstream visuals stay
          frame-accurate. Use it to leave room for a visual to land.
        * `voice` per beat enables multi-speaker scripts (podcasts, interviews):
          each beat may name a different registered voice.
        * `target_duration_sec` + `fit_mode` ("none"|"pad"|"atempo") force a beat
          into a fixed slot, e.g. for a video timeline.

        Returns the master audio `file_path` plus per-beat start/end times
        (`beats[]` with `pause_after_sec` and the `voice_id` that actually spoke),
        and SRT/VTT paths when `export_srt` is true.
        """
        from .api.routes_agent import _render_script

        # Reuse the REST implementation so the two surfaces cannot diverge.
        fake_request = _FakeRequest(ctx, _agent_id())
        try:
            payload = ScriptRequest(
                beats=[BeatSpec(**b) for b in beats],
                default_voice=default_voice,
                engine=engine,
                quality=quality,  # type: ignore[arg-type]
                format=format,  # type: ignore[arg-type]
                export_srt=export_srt,
            )
            result = await _render_script(fake_request, payload)
            return result.model_dump(mode="json")
        except AgentVoiceError as exc:
            return _error(exc)

    @mcp.tool
    async def start_script_job(
        beats: list[dict[str, Any]],
        default_voice: str | None = None,
        engine: str | None = None,
        quality: str = "high",
        format: str = "wav",
        export_srt: bool = True,
    ) -> dict[str, Any]:
        """Start a script render in the background. Poll get_job() for the result.

        Takes the same `beats` (and the same per-beat keys) as speak_script:
        {text, voice?, engine?, model?, quality?, language?,
         target_duration_sec?, fit_mode?, pause_after_sec?}

        `pause_after_sec` (0..10) is silence inserted after a beat and included in
        the reported timings; per-beat `voice` enables multi-speaker scripts;
        `target_duration_sec` + `fit_mode` fit a beat into a fixed video slot.

        Use this for long scripts so the tool call does not block: it returns a
        `job_id` immediately and get_job() eventually yields the ScriptResult.
        """
        from .api.routes_agent import _render_script

        try:
            payload = ScriptRequest(
                beats=[BeatSpec(**b) for b in beats],
                default_voice=default_voice,
                engine=engine,
                quality=quality,  # type: ignore[arg-type]
                format=format,  # type: ignore[arg-type]
                export_srt=export_srt,
            )
            job = ctx.jobs.create("script")
            fake_request = _FakeRequest(ctx, _agent_id())

            async def runner() -> None:
                ctx.jobs.mark_running(job.job_id)
                try:
                    result = await _render_script(fake_request, payload)
                    ctx.jobs.succeeded(job.job_id, result.model_dump(mode="json"))
                except AgentVoiceError as exc:
                    ctx.jobs.failed(job.job_id, exc.to_response())
                except asyncio.CancelledError:
                    ctx.jobs.update(job.job_id, status="cancelled")
                    raise
                except Exception as exc:
                    ctx.jobs.failed(
                        job.job_id,
                        ErrorResponse(error_code=ErrorCode.INTERNAL_ERROR, message=str(exc)),
                    )

            ctx.jobs.register_task(job.job_id, asyncio.create_task(runner()))
            return {"job_id": job.job_id, "kind": "script"}
        except AgentVoiceError as exc:
            return _error(exc)

    @mcp.tool
    async def get_job(job_id: str) -> dict[str, Any]:
        """Fetch the status and result of an async job."""
        try:
            return ctx.jobs.get(job_id).model_dump(mode="json")
        except AgentVoiceError as exc:
            return _error(exc)

    @mcp.tool
    async def cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel a running job. Already-finished jobs are returned unchanged."""
        try:
            return ctx.jobs.cancel(job_id).model_dump(mode="json")
        except AgentVoiceError as exc:
            return _error(exc)

    # -- voices ---------------------------------------------------------
    @mcp.tool
    async def clone_voice(
        name: str,
        ref_audio_path: str,
        ref_text: str = "",
        x_vector_only: bool = False,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Register a reference voice from a local audio file.

        The clip is normalised to 24 kHz mono and silence-trimmed. 3-10 seconds is
        the quality sweet spot. Providing `ref_text` (an accurate transcript) gives
        noticeably better cloning than x_vector_only mode: the engine then runs in
        ICL mode and conditions on both the audio and its words.

        Idempotent: retrying with the same `name` and byte-identical audio returns
        the existing voice instead of creating a duplicate, so a retried call is
        safe. If a voice with that name exists but the audio (or transcript)
        differs, the call fails rather than silently swapping a voice an agent is
        already using — pass overwrite=true to replace it.
        """
        from pathlib import Path

        from .security import ensure_within_base

        try:
            base = ctx.settings.mcp_base_path
            source = (
                ensure_within_base(ref_audio_path, base)
                if base
                else Path(ref_audio_path).expanduser()
            )
            metadata = ctx.voices.add(
                name=name,
                source_path=source,
                ref_text=ref_text,
                x_vector_only=x_vector_only,
                overwrite=overwrite,
            )
            return metadata.model_dump(mode="json")
        except AgentVoiceError as exc:
            return _error(exc)

    @mcp.tool
    async def list_voices() -> dict[str, Any]:
        """List registered voices with duration and any quality warnings."""
        return {"voices": [v.model_dump(mode="json") for v in ctx.voices.list()]}

    @mcp.tool
    async def delete_voice(voice_id: str) -> dict[str, Any]:
        """Delete a voice and all of its cached prompts."""
        if not ctx.voices.delete(voice_id):
            return _error(
                AgentVoiceError(
                    ErrorCode.VOICE_NOT_FOUND,
                    f"no such voice: {voice_id}",
                    "Call list_voices() to see available ids.",
                )
            )
        return {"deleted": voice_id}

    # -- bindings -------------------------------------------------------
    @mcp.tool
    async def set_agent_binding(
        agent_id: str,
        voice_id: str | None = None,
        engine_id: str | None = None,
        model_alias: str | None = None,
    ) -> dict[str, Any]:
        """Route this agent to a default voice/engine/model.

        `agent_id` here is convenience routing on a local single-user box, not an
        authorization boundary.
        """
        try:
            if engine_id:
                ctx.registry.get(engine_id)
            binding = ctx.bindings.set(
                agent_id, voice_id=voice_id, engine_id=engine_id, model_alias=model_alias
            )
            return binding.model_dump(mode="json")
        except AgentVoiceError as exc:
            return _error(exc)

    @mcp.tool
    async def list_agent_bindings() -> dict[str, Any]:
        """List all agent bindings."""
        return {"bindings": [b.model_dump(mode="json") for b in ctx.bindings.list()]}

    # -- discovery ------------------------------------------------------
    @mcp.tool
    async def list_engines() -> dict[str, Any]:
        """List engines with capabilities, install state and why_unavailable."""
        entries = ctx.registry.list_entries(loaded=ctx.workers.loaded_models())
        return {"engines": [e.model_dump(mode="json") for e in entries]}

    @mcp.tool
    async def list_models(engine_id: str | None = None) -> dict[str, Any]:
        """List models across engines with HF repo, download and loaded state."""
        loaded = ctx.workers.loaded_models()
        payload = []
        for model in ctx.registry.list_models(engine_id):
            entry = model.model_dump(mode="json")
            entry["loaded"] = model.alias in loaded.get(model.engine_id, [])
            payload.append(entry)
        return {"models": payload}

    @mcp.tool
    async def download_model(engine_id: str, model_alias: str) -> dict[str, Any]:
        """Start an async HuggingFace download. Poll get_job() for progress."""
        from .api.routes_engines import _run_download

        try:
            if not ctx.settings.allow_agent_engine_mutation:
                raise AgentVoiceError(
                    ErrorCode.AGENT_MUTATION_DISABLED,
                    "agent-driven model download is disabled",
                    "Set ALLOW_AGENT_ENGINE_MUTATION=true to enable it.",
                )
            ctx.registry.model_spec(engine_id, model_alias)
            job = ctx.jobs.create("download_model")
            ctx.jobs.register_task(
                job.job_id,
                asyncio.create_task(
                    _run_download(ctx, engine_id, model_alias, job.job_id)
                ),
            )
            return {"job_id": job.job_id, "kind": "download_model"}
        except AgentVoiceError as exc:
            return _error(exc)

    @mcp.tool
    async def register_model(
        engine_id: str,
        model_alias: str,
        hf_repo: str,
        quality_tier: str = "high",
        est_vram_gb: float = 4.4,
        download: bool = True,
    ) -> dict[str, Any]:
        """Add a model to an engine's manifest and hot-reload the registry.

        Guarded by ALLOW_AGENT_ENGINE_MUTATION on loopback. Optionally starts an
        async download.
        """
        from .api.routes_engines import _run_download

        try:
            if not ctx.settings.allow_agent_engine_mutation:
                raise AgentVoiceError(
                    ErrorCode.AGENT_MUTATION_DISABLED,
                    "agent-driven manifest mutation is disabled",
                    "Set ALLOW_AGENT_ENGINE_MUTATION=true to enable it.",
                )
            ctx.registry.register_model(
                engine_id=engine_id,
                model_alias=model_alias,
                hf_repo=hf_repo,
                quality_tier=quality_tier,
                est_vram_gb=est_vram_gb,
            )
            job_id = None
            if download:
                job = ctx.jobs.create("download_model")
                job_id = job.job_id
                ctx.jobs.register_task(
                    job_id,
                    asyncio.create_task(
                        _run_download(ctx, engine_id, model_alias, job_id)
                    ),
                )
            return {
                "engine_id": engine_id,
                "model_alias": model_alias,
                "job_id": job_id,
            }
        except AgentVoiceError as exc:
            return _error(exc)

    @mcp.tool
    async def register_engine(
        engine_id: str,
        engine_yaml_content: str,
        pyproject_toml_content: str | None = None,
        worker_py_content: str | None = None,
        install_venv: bool = True,
    ) -> dict[str, Any]:
        """Scaffold a new engine plugin and hot-reload without restarting the daemon.

        `engine_id` must be a lowercase slug. Path traversal is rejected. Guarded
        by ALLOW_AGENT_ENGINE_MUTATION on loopback.
        """
        from .api.routes_engines import _run_install

        try:
            if not ctx.settings.allow_agent_engine_mutation:
                raise AgentVoiceError(
                    ErrorCode.AGENT_MUTATION_DISABLED,
                    "agent-driven engine registration is disabled",
                    "Set ALLOW_AGENT_ENGINE_MUTATION=true to enable it.",
                )
            try:
                record = ctx.registry.register_engine(
                    engine_id=engine_id,
                    engine_yaml_content=engine_yaml_content,
                    pyproject_toml_content=pyproject_toml_content,
                    worker_py_content=worker_py_content,
                )
            except ValueError as exc:
                # A bad slug surfaces as a pydantic ValidationError, which would
                # otherwise escape as an unstructured MCP tool exception.
                raise AgentVoiceError(
                    ErrorCode.INVALID_SLUG,
                    f"invalid engine id {engine_id!r}: {exc}",
                    "Use a lowercase slug: letters, digits and underscores, 2-41 chars.",
                ) from exc
            job_id = None
            if install_venv and (record.directory / "pyproject.toml").is_file():
                job = ctx.jobs.create("install_engine")
                job_id = job.job_id
                ctx.jobs.register_task(
                    job_id,
                    asyncio.create_task(_run_install(ctx, engine_id, job_id)),
                )
            return {
                "engine_id": engine_id,
                "installed": ctx.registry.get(engine_id).installed,
                "job_id": job_id,
            }
        except AgentVoiceError as exc:
            return _error(exc)

    @mcp.tool
    async def install_engine(engine_id: str) -> dict[str, Any]:
        """Run `uv sync` for an engine and verify the IPC handshake."""
        from .api.routes_engines import _run_install

        try:
            if not ctx.settings.allow_agent_engine_mutation:
                raise AgentVoiceError(
                    ErrorCode.AGENT_MUTATION_DISABLED,
                    "agent-driven engine install is disabled",
                    "Set ALLOW_AGENT_ENGINE_MUTATION=true to enable it.",
                )
            ctx.registry.get(engine_id)
            job = ctx.jobs.create("install_engine")
            ctx.jobs.register_task(
                job.job_id,
                asyncio.create_task(_run_install(ctx, engine_id, job.job_id)),
            )
            return {"job_id": job.job_id, "kind": "install_engine"}
        except AgentVoiceError as exc:
            return _error(exc)

    # -- GPU lifecycle --------------------------------------------------
    @mcp.tool
    async def gpu_status() -> dict[str, Any]:
        """Report worker state, resident models, VRAM and the idle-unload countdown."""
        return await ctx.workers.gpu_status()

    @mcp.tool
    async def unload_models(force: bool = False) -> dict[str, Any]:
        """Free VRAM.

        Returns WORKER_BUSY if jobs are running and `force` is false; pass
        force=true to cancel them and free the GPU immediately.
        """
        try:
            return await ctx.workers.unload_models(force=force)
        except AgentVoiceError as exc:
            payload = _error(exc)
            payload["details"] = exc.details
            return payload

    return mcp


class _FakeRequest:
    """Minimal stand-in so MCP tools can reuse the REST script implementation."""

    def __init__(self, ctx, agent_id: str | None) -> None:
        self.app_state = type("S", (), {"ctx": ctx})()
        self.headers = {AGENT_ID_HEADER: agent_id} if agent_id else {}

    @property
    def app(self):
        return type("A", (), {"state": self.app_state})()

    @property
    def client(self):
        return None
