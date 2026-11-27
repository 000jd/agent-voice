"""MCP server: the agent-facing surface.

Audio is never inlined as base64 — every synthesis tool returns a ``file_path``
the caller can read, materialised inside ``MCP_BASE_PATH/artifacts`` when that is
configured so the path is reachable from a sandboxed agent.

Three facts about this transport shape the code below:

1. ``X-Agent-Id`` is read from the live HTTP request. ``get_http_headers()``
   withholds ``authorization`` by default, which is exactly why Bearer auth is
   enforced by FastAPI middleware instead of in here.
2. Errors are raised, never returned. ``fastmcp`` catches a ``ToolError`` and
   answers ``CallToolResult(isError=True)`` with ``str(e)`` as the text, so
   raising preserves the JSON ``{error_code, message, suggestion}`` body *and*
   lets a client tell failure from success without parsing the payload.
3. Mutation tools read the real peer address from the live request. They used to
   go through a fake ``Request`` whose ``client`` was ``None``, which silently
   disabled the loopback check — so ``services.script_service.render_script`` is
   now called with an explicit ``agent_id`` instead.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, NoReturn

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers, get_http_request
from mcp_types import ToolAnnotations

from .pronunciations import check_pronunciation_map
from .schemas import (
    DEFAULT_DESIGN_SAMPLE_TEXT,
    AgentVoiceError,
    BeatSpec,
    DesignVoiceRequest,
    DesignVoiceResult,
    DubPrepareRequest,
    DubRenderRequest,
    DubSegmentSpec,
    EngineEntry,
    ErrorCode,
    ErrorResponse,
    QualityTier,
    ResponseFormat,
    ScriptRequest,
    ScriptResult,
    SpeakRequest,
    SynthesisResult,
    TranscribeRequest,
    VoiceListResponse,
)
from .security import is_loopback_client, resolve_media_path, resolve_ref_audio_path
from .services.script_service import render_script
from .services.voice_design_service import render_design
from .settings_types import SettingsLike

AGENT_ID_HEADER = "x-agent-id"

SKILL_DOC_RELATIVE = Path("skills") / "agent-voice" / "SKILL.md"

GUIDE_FALLBACK = (
    "# agent-voice\n\n"
    "The full guide is missing from this install (skills/agent-voice/SKILL.md).\n\n"
    "Call server_info() first: it is free and reports voices_count (0 means clone a "
    "voice first), the installed engines and the GPU state. Then speak() for one "
    "utterance, or speak_script() for multi-beat content with timings. Every error "
    "carries a `suggestion` naming the exact next call.\n"
)


def _agent_id() -> str | None:
    """Selective-routing id from the live request, if the client sent one."""
    headers = get_http_headers()
    return headers.get(AGENT_ID_HEADER)


def _client_host() -> str | None:
    """Peer address of the live MCP request, or None when there is no HTTP request.

    None means the stdio transport. That is inherently local — the ``agent-voice
    mcp`` shim runs on this machine and proxies to a loopback daemon — so a call
    with no HTTP request cannot have arrived from the network. Treating it as
    local is what lets a stdio-only agent keep full functionality.
    """
    try:
        request = get_http_request()
    except RuntimeError:
        return None
    client = getattr(request, "client", None)
    return getattr(client, "host", None)


def _raise_tool_error(exc: AgentVoiceError) -> NoReturn:
    """Re-raise an AgentVoiceError as a real MCP tool error (``isError=true``).

    The text is the canonical ``{error_code, message, suggestion}`` JSON so an
    agent gets the same actionable body it would from REST, while the transport
    flag is honest about the call having failed.
    """
    raise ToolError(json.dumps(exc.to_response().model_dump(mode="json")))


def _require_mutation_allowed(settings: SettingsLike, tool_name: str) -> None:
    """Gate an engine-tree mutation on the opt-in flag and on both ends being local.

    Registering an engine writes Python that the daemon later executes, so it is
    a code-execution primitive: it may never be reachable from off-box. Both
    ends are checked — the bind address says whether the daemon is exposed at
    all, the peer address says who is actually calling — and either being
    non-loopback is enough to refuse.
    """
    if not settings.allow_agent_engine_mutation:
        raise AgentVoiceError(
            ErrorCode.AGENT_MUTATION_DISABLED,
            f"{tool_name} is disabled: agent-driven engine mutation is off",
            "Set ALLOW_AGENT_ENGINE_MUTATION=true and keep the daemon bound to "
            "127.0.0.1 to let agents add engines, models or downloads.",
        )
    if not is_loopback_client(settings.host):
        raise AgentVoiceError(
            ErrorCode.AGENT_MUTATION_DISABLED,
            f"{tool_name} requires a loopback daemon (bound to {settings.host})",
            "Bind the daemon to 127.0.0.1 to let agents manage engines and models.",
        )
    host = _client_host()
    if host is not None and not is_loopback_client(host):
        raise AgentVoiceError(
            ErrorCode.AGENT_MUTATION_DISABLED,
            f"{tool_name} is loopback-only (caller was {host})",
            "Run the MCP client on the daemon's machine, or manage engines with the CLI.",
        )


def _with_default_pause(beats: list[BeatSpec], pause_after_sec: float) -> list[BeatSpec]:
    """Apply a per-call default pause to beats that did not choose their own.

    ``pause_after_sec`` is a per-beat field, but most callers want one rhythm for
    the whole script. A beat that sets its own pause keeps it — an explicit value
    always wins over the call-level default.
    """
    if pause_after_sec <= 0:
        return beats
    return [
        beat if beat.pause_after_sec > 0 else beat.model_copy(
            update={"pause_after_sec": pause_after_sec}
        )
        for beat in beats
    ]


def _check_pronunciations(pronunciations: dict[str, str] | None) -> None:
    """Reject an oversized per-call map as a structured tool error."""
    check_pronunciation_map(pronunciations)


def _preset_speech_surface(
    entries: list[EngineEntry], default_engine: str
) -> tuple[list[str], bool]:
    """Preset speakers and voice-design availability, from manifests only.

    Both answers are read from the installed engines' model metadata, never from
    a worker, which is what keeps ``server_info()`` free and GPU-free. A model
    counts as usable when its weights are downloaded or it ships as a ``local/``
    repo; a listed-but-absent checkpoint cannot speak, so it must not advertise
    a speaker the caller cannot actually use.

    Scoped to the default engine on purpose: ``speak(speaker=...)`` without an
    explicit engine resolves there, so advertising a speaker from another engine
    would be rejected by the very call this list is meant to enable.
    """
    speakers: list[str] = []
    design_available = False
    for entry in entries:
        if not entry.installed or entry.engine_id != default_engine:
            continue
        for model in entry.models:
            if not (model.downloaded_locally or model.hf_repo.startswith("local/")):
                continue
            if model.mode == "custom_voice":
                for name in model.preset_speakers:
                    if name not in speakers:
                        speakers.append(name)
            elif model.mode == "voice_design":
                design_available = True
    return speakers, design_available


def _design_checkpoint_step(
    default: EngineEntry | None, settings: SettingsLike
) -> str:
    """How to get a voice_design checkpoint, given what the default engine lists.

    Naming the exact ``models download`` invocation is the difference between an
    agent that can fix a clone-only install and one that only learns that
    ``design_voice()`` is unavailable.
    """
    design = next(
        (
            model
            for model in (default.models if default is not None else [])
            if model.mode == "voice_design"
        ),
        None,
    )
    if design is None:
        return (
            "voice design is unavailable: the "
            f"{settings.default_engine} manifest lists no `voice_design` checkpoint."
        )
    return (
        "voice design needs its checkpoint downloaded: run "
        f"`agent-voice models download {settings.default_engine} {design.alias}`."
    )


def _next_steps(
    entries: list[EngineEntry],
    settings: SettingsLike,
    *,
    voices_count: int,
    preset_speakers: list[str],
    voice_design_available: bool = False,
) -> list[str]:
    """Actionable advice for a server that cannot speak yet; empty when ready.

    Reads manifests only, like the rest of ``server_info()``: it must never spawn
    a worker or touch the GPU. A tool that cannot run is never recommended:
    ``design_voice()`` is named only when a ``voice_design`` checkpoint is
    actually usable on the default engine, and a clone-only install is told how
    to get one instead.
    """
    default = next(
        (entry for entry in entries if entry.engine_id == settings.default_engine), None
    )
    steps: list[str] = []
    if default is None or not default.installed:
        steps.append(f"Run `agent-voice engines install {settings.default_engine}`.")
    elif not any(
        model.downloaded_locally or model.hf_repo.startswith("local/")
        for model in default.models
    ):
        alias = default.models[0].alias if default.models else None
        download = f" {settings.default_engine} {alias}" if alias else ""
        steps.append(
            f"No {settings.default_engine} weights are cached: run "
            f"`agent-voice models download{download}` or `agent-voice doctor`."
        )
    if not voices_count and not preset_speakers:
        clone = (
            f"Put a 3-10 s reference clip in {settings.inbox_dir} and call "
            "clone_voice()"
        )
        if voice_design_available:
            steps.append(f"{clone}, or call design_voice().")
        else:
            steps.append(f"{clone}; {_design_checkpoint_step(default, settings)}")
    return steps


def _asr_surface(entries: list[EngineEntry]) -> list[dict[str, Any]]:
    """The ``kind: asr`` engines and what each can do, from manifests only.

    Same rule as the preset-speaker list: a capability is advertised only when
    the engine that would answer the call declares it, so ``transcribe()`` never
    gets a ``hotwords`` list or a ``word_timestamps`` flag it cannot honour.
    """
    return [
        {
            "engine_id": entry.engine_id,
            "installed": entry.installed,
            "languages": entry.languages,
            "word_timestamps": entry.word_timestamps,
            "hotwords": entry.hotwords,
        }
        for entry in entries
        if entry.kind == "asr"
    ]


def _dubbing_available(entries: list[EngineEntry]) -> bool:
    """True only when both halves of a dub can actually run.

    Dubbing is ASR plus TTS in one job, so an installed speaker with no
    transcriber (or vice versa) is a dead end worth reporting before the caller
    spends a transcription to discover it.
    """
    return any(e.installed and e.kind == "asr" for e in entries) and any(
        e.installed and e.kind == "tts" for e in entries
    )


def _guide_text() -> str:
    """Read the agent guide, falling back to a short summary when it is absent.

    Two roots are tried because the daemon can be started from the repo root or
    from anywhere at all (systemd, an MCP client's cwd, a container): cwd covers
    an explicit checkout, walking up from ``__file__`` covers a run started
    elsewhere. A missing guide must not turn a documentation resource into an
    error the agent has to handle, hence the fallback instead of a raise.
    """
    candidates = [Path.cwd() / SKILL_DOC_RELATIVE]
    candidates += [parent / SKILL_DOC_RELATIVE for parent in Path(__file__).resolve().parents]
    for candidate in candidates:
        try:
            return candidate.read_text("utf-8")
        except OSError:
            continue
    return GUIDE_FALLBACK


def build_mcp_server(ctx) -> FastMCP:
    settings: SettingsLike = ctx.settings

    # `auto` infers the surface from the security posture: registering engines and
    # models writes Python the daemon will execute, so those tools are hidden
    # unless an operator opted in — a tool that can only answer
    # AGENT_MUTATION_DISABLED is noise in the model's tool list. `core` pins them
    # off even when mutation is enabled (a short list costs fewer tokens and has
    # less for a model to get wrong), `full` pins them on so the per-call guard
    # can answer with an actionable error instead of the tool being absent.
    full = settings.toolset == "full" or (
        settings.toolset == "auto" and settings.allow_agent_engine_mutation
    )

    instructions = (
        "Local voice cloning and speech synthesis. Call server_info() FIRST: "
        "it is free (no GPU wake) and tells you the version, whether any voice "
        "exists, which voices are registered and which engines are installed. "
        "Then call speak() for one utterance, speak_script() for multi-beat "
        "content with timings. Use transcribe() for speech recognition and the "
        "dub_prepare()/dub_render() pair to dub a video into another language. "
        "Tools return file paths, never audio blobs. "
        "Use gpu_status() to see what is resident and unload_models() to free "
        "VRAM when another GPU application needs it."
    )
    if full:
        instructions += (
            " Engine and model management is enabled (register_engine, "
            "register_model, download_model, install_engine). Those tools rewrite "
            "the plugin tree and are refused unless the daemon and the caller are "
            "both on this machine."
        )
    else:
        instructions += (
            " This server exposes the core toolset only: installing engines or "
            "models needs an operator to set ALLOW_AGENT_ENGINE_MUTATION=true."
        )

    mcp: FastMCP = FastMCP("agent-voice", instructions=instructions)

    # One decorator for both registration and hints. `full_only` tools are not
    # registered at all in the core toolset, so the model never sees a tool it
    # cannot use.
    def tool(*, annotations: ToolAnnotations, full_only: bool = False):
        def decorate(fn):
            if full_only and not full:
                return fn
            return mcp.tool(annotations=annotations)(fn)

        return decorate

    read_only = ToolAnnotations(read_only_hint=True)
    idempotent = ToolAnnotations(idempotent_hint=True)
    destructive = ToolAnnotations(destructive_hint=True)
    open_world_mutation = ToolAnnotations(destructive_hint=True, open_world_hint=True)

    # -- orientation ----------------------------------------------------
    # Deliberately free of error handling that could change the shape: the
    # registry and voice-store queries are pure reads of on-disk state, so the key
    # set documented below is always exactly what the caller gets back.
    @tool(annotations=read_only)
    async def server_info() -> dict[str, Any]:
        """Call this first: a free, GPU-free snapshot of what this machine can do.

        Read: version, default_engine, default_asr_engine, default_voice,
        voices_count, voices[], inbox_dir, preset_speakers[],
        voice_design_available, installed_engines[], engines[], asr_engines[],
        dubbing_available, gpu_status and next_steps[].
        """
        from . import __version__

        entries = ctx.registry.list_entries(loaded=ctx.workers.loaded_models())
        voices = ctx.voices.list()
        preset_speakers, voice_design_available = _preset_speech_surface(
            entries, settings.default_engine
        )
        default_voice = settings.default_voice_id or None
        if default_voice is None and len(voices) == 1:
            # A single registered voice is unambiguous: use it rather than
            # making the caller discover and pass its id.
            default_voice = voices[0].voice_id
        return {
            "version": __version__,
            "default_engine": settings.default_engine,
            "default_asr_engine": settings.default_asr_engine,
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
            "inbox_dir": str(settings.inbox_dir),
            "preset_speakers": preset_speakers,
            "voice_design_available": voice_design_available,
            "next_steps": _next_steps(
                entries,
                settings,
                voices_count=len(voices),
                preset_speakers=preset_speakers,
                voice_design_available=voice_design_available,
            ),
            "engines": [
                {
                    "engine_id": e.engine_id,
                    "installed": e.installed,
                    "streaming": e.streaming,
                    "why_unavailable": e.why_unavailable,
                }
                for e in entries
            ],
            "asr_engines": _asr_surface(entries),
            "dubbing_available": _dubbing_available(entries),
            "gpu_status": await ctx.workers.gpu_status(),
        }

    # -- synthesis ------------------------------------------------------
    @tool(annotations=idempotent)
    async def speak(
        text: str,
        voice: str | None = None,
        speaker: str | None = None,
        instruct: str = "",
        engine: str | None = None,
        model: str | None = None,
        quality: QualityTier = "high",
        language: str = "Auto",
        format: ResponseFormat = "wav",
        seed: int | None = None,
        speed: float = 1.0,
        take: int | None = None,
        pronunciations: dict[str, str] | None = None,
        markup: bool = True,
        spell_identifiers: bool = False,
        paragraph_gap_ms: int = 0,
        project: str | None = None,
    ) -> SynthesisResult:
        """Synthesise ONE utterance to a file; use speak_script() for several beats.

        Read: file_path (read this; audio is never inlined), duration_sec, cached
        (a repeat is free), elapsed_ms, and realtime_multiple = duration_sec /
        elapsed_sec (0.86 = slightly slower than real time, 7.1 = ~7x faster).
        real_time_factor is a deprecated alias of realtime_multiple.

        Args:
            take: Alternate delivery of the same line, with a repeatable seed.
            pronunciations: Per-call word -> spoken-form overrides.
            markup: Interpret [pause]/[slow]/[fast]/[emphasis]/[spell] tags.
            spell_identifiers: Expand code-like tokens before synthesis.
            paragraph_gap_ms: Silence at each blank-line paragraph break.
            project: Scoped-lexicon project slug.
        """
        from .api.routing import resolve_target
        from .services.utterance_service import speak_utterance

        try:
            _check_pronunciations(pronunciations)
            request = SpeakRequest(
                text=text,
                voice=voice,
                speaker=speaker,
                instruct=instruct,
                engine=engine,
                model=model,
                quality=quality,
                language=language,
                format=format,
                seed=seed,
                speed=speed,
                take=take,
                pronunciations=pronunciations,
                markup=markup,
                spell_identifiers=spell_identifiers,
                paragraph_gap_ms=paragraph_gap_ms,
                project=project,
            )
            if len(request.text) > settings.max_text_chars:
                raise AgentVoiceError(
                    ErrorCode.TEXT_TOO_LONG,
                    f"text is {len(request.text)} characters, limit is "
                    f"{settings.max_text_chars}",
                    "Split it up or use speak_script.",
                )

            def resolve():
                return resolve_target(
                    settings=settings,
                    registry=ctx.registry,
                    voices=ctx.voices,
                    bindings=ctx.bindings,
                    agent_id=_agent_id(),
                    voice=request.voice,
                    speaker=request.speaker,
                    instruct=request.instruct,
                    engine=request.engine,
                    model=request.model,
                    quality=request.quality,
                )

            # The same pipeline the REST and script surfaces use. Speaking
            # through a second copy of the rewrite rules is how MCP and REST
            # would drift; the admission caps above still apply to the raw text.
            outcome = await speak_utterance(
                ctx,
                text=request.text,
                resolve=resolve,
                markup=request.markup,
                spell_identifiers=request.spell_identifiers,
                paragraph_gap_ms=request.paragraph_gap_ms,
                project=request.project,
                overrides=request.pronunciations,
                language=request.language,
                seed=request.seed,
                speed=request.speed,
                response_format=request.format,
                take=request.take,
            )
            return outcome.result
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    @tool(annotations=idempotent)
    async def speak_script(
        beats: list[BeatSpec],
        default_voice: str | None = None,
        engine: str | None = None,
        quality: QualityTier = "high",
        format: ResponseFormat = "wav",
        export_srt: bool = True,
        pause_after_sec: float = 0.0,
        pronunciations: dict[str, str] | None = None,
        markup: bool = True,
        spell_identifiers: bool = False,
        paragraph_gap_ms: int = 0,
        project: str | None = None,
    ) -> ScriptResult:
        """Synthesise a multi-beat script into one stitched file with exact timings.

        Read: file_path, duration_sec, beats[] (start_sec/end_sec) and, when
        export_srt, srt_path and timing_json_path. Each beat is a BeatSpec, so
        per-beat voice, speaker, instruct, pause_after_sec and fit_mode are honoured.
        """
        try:
            _check_pronunciations(pronunciations)
            payload = ScriptRequest(
                beats=_with_default_pause(beats, pause_after_sec),
                default_voice=default_voice,
                engine=engine,
                quality=quality,
                format=format,
                export_srt=export_srt,
                pronunciations=pronunciations,
                markup=markup,
                spell_identifiers=spell_identifiers,
                paragraph_gap_ms=paragraph_gap_ms,
                project=project,
            )
            return await render_script(ctx, payload, agent_id=_agent_id())
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    @tool(annotations=ToolAnnotations(idempotent_hint=False, destructive_hint=False))
    async def start_script_job(
        beats: list[BeatSpec],
        default_voice: str | None = None,
        engine: str | None = None,
        quality: QualityTier = "high",
        format: ResponseFormat = "wav",
        export_srt: bool = True,
        pause_after_sec: float = 0.0,
        pronunciations: dict[str, str] | None = None,
        markup: bool = True,
        spell_identifiers: bool = False,
        paragraph_gap_ms: int = 0,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Start a script render in the background and return a job id immediately.

        Same beats and options as speak_script(); poll get_job() for the result.
        """
        try:
            _check_pronunciations(pronunciations)
            payload = ScriptRequest(
                beats=_with_default_pause(beats, pause_after_sec),
                default_voice=default_voice,
                engine=engine,
                quality=quality,
                format=format,
                export_srt=export_srt,
                pronunciations=pronunciations,
                markup=markup,
                spell_identifiers=spell_identifiers,
                paragraph_gap_ms=paragraph_gap_ms,
                project=project,
            )
            job = ctx.jobs.create("script")
            # Captured before the task starts: get_http_headers() is only valid
            # inside the request that triggered the call, not in the background task.
            agent_id = _agent_id()

            async def runner() -> None:
                ctx.jobs.mark_running(job.job_id)
                try:
                    # Progress per beat, not per job: nine beats that jump
                    # 0.01 -> 1.0 tell a polling agent nothing about how long to wait.
                    result = await render_script(
                        ctx,
                        payload,
                        agent_id=agent_id,
                        on_beat=lambda done, total, message: ctx.jobs.update(
                            job.job_id, progress=done / total, message=message
                        ),
                    )
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
            return {"job_id": job.job_id, "kind": "script"}
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    @tool(annotations=read_only)
    async def get_job(job_id: str) -> dict[str, Any]:
        """Poll a job until it leaves pending/running, then read the outcome.

        Read: status, progress 0..1, `result` once succeeded and `error` when failed.
        """
        try:
            return ctx.jobs.get(job_id).model_dump(mode="json")
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    @tool(annotations=destructive)
    async def cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel a running job; one that already finished is returned unchanged."""
        try:
            return ctx.jobs.cancel(job_id).model_dump(mode="json")
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    # -- voices ---------------------------------------------------------
    @tool(annotations=idempotent)
    async def clone_voice(
        name: str,
        ref_audio_path: str,
        ref_text: str = "",
        x_vector_only: bool = False,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Register a reference voice from a local audio file. Do this before speaking.

        3-10 s of clean single-speaker speech with an accurate ref_text clones best;
        without MCP_BASE_PATH the clip must be in the inbox. Read: voice_id (pass as
        `voice`) and warnings[].
        """
        try:
            # The same resolver REST uses, so a relative path is refused here too
            # even when ALLOW_ANY_PATH is on: the daemon's cwd is not the caller's.
            source = resolve_ref_audio_path(
                ref_audio_path,
                mcp_base_path=settings.mcp_base_path,
                inbox_dir=settings.inbox_dir,
                allow_any_path=settings.allow_any_path,
            )
            return ctx.voices.add(
                name=name,
                source_path=source,
                ref_text=ref_text,
                x_vector_only=x_vector_only,
                overwrite=overwrite,
            ).model_dump(mode="json")
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    @tool(annotations=ToolAnnotations(idempotent_hint=False, destructive_hint=False))
    async def design_voice(
        name: str,
        instruct: str,
        sample_text: str = DEFAULT_DESIGN_SAMPLE_TEXT,
        language: str = "English",
        save_as_clone: bool = True,
        take: int | None = None,
        seed: int | None = None,
        overwrite: bool = False,
        engine: str | None = None,
        model: str | None = None,
        quality: QualityTier = "high",
        candidates: int = 1,
    ) -> DesignVoiceResult:
        """Design a new voice from a written description; no reference clip needed.

        Use it when no preset speaker or cloned voice fits; candidates>1 returns
        several takes to audition. Read: voice_id (pass as `voice`) and
        preview_file_path.
        """
        try:
            if not 1 <= candidates <= 5:
                raise AgentVoiceError(
                    ErrorCode.BAD_REQUEST,
                    f"candidates must be between 1 and 5, got {candidates}",
                    "Ask for candidates=1 when a single voice is enough.",
                )
            try:
                payload = DesignVoiceRequest(
                    name=name,
                    instruct=instruct,
                    sample_text=sample_text,
                    language=language,
                    save_as_clone=save_as_clone,
                    candidates=candidates,
                    take=take,
                    seed=seed,
                    overwrite=overwrite,
                    engine=engine,
                    model=model,
                    quality=quality,
                )
            except ValueError as exc:
                # A pydantic ValidationError is a ValueError: an unusable name or
                # take must still surface as the canonical JSON error body rather
                # than an unstructured tool exception.
                raise AgentVoiceError(
                    ErrorCode.BAD_REQUEST,
                    f"invalid voice design request: {exc}",
                    "Use a name of 1-64 letters, digits, spaces, '.', '-' or '_'.",
                ) from exc
            return await render_design(ctx, payload, agent_id=_agent_id())
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    @tool(annotations=read_only)
    async def list_voices() -> VoiceListResponse:
        """List every registered voice with id, name, duration, source and warnings."""
        return VoiceListResponse(voices=ctx.voices.list())

    @tool(annotations=destructive)
    async def delete_voice(voice_id: str) -> dict[str, str]:
        """Delete a voice and its cached prompts. This cannot be undone."""
        if not ctx.voices.delete(voice_id):
            _raise_tool_error(
                AgentVoiceError(
                    ErrorCode.VOICE_NOT_FOUND,
                    f"no such voice: {voice_id}",
                    "Call list_voices() to see available ids.",
                )
            )
        return {"deleted": voice_id}

    # -- bindings -------------------------------------------------------
    @tool(annotations=ToolAnnotations(idempotent_hint=True, destructive_hint=False), full_only=True)
    async def set_agent_binding(
        agent_id: str,
        voice_id: str | None = None,
        engine_id: str | None = None,
        model_alias: str | None = None,
    ) -> dict[str, Any]:
        """Route this agent to a default voice/engine/model. Idempotent.

        Use it when several agents share the daemon and each should keep its own
        default, so later calls can omit `voice`/`engine`. `agent_id` here is
        convenience routing on a local single-user box, not an authorization
        boundary.

        Read:
            agent_id, voice_id, engine_id, model_alias: the binding now in effect.
        """
        try:
            if engine_id:
                ctx.registry.get(engine_id)
            return ctx.bindings.set(
                agent_id, voice_id=voice_id, engine_id=engine_id, model_alias=model_alias
            ).model_dump(mode="json")
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    @tool(annotations=read_only, full_only=True)
    async def list_agent_bindings() -> dict[str, Any]:
        """List every agent binding. Use it to check what a caller will inherit.

        Read:
            bindings[].agent_id: the X-Agent-Id header the binding applies to.
            bindings[].voice_id, bindings[].engine_id, bindings[].model_alias: what
                that agent is pinned to.
        """
        return {"bindings": [b.model_dump(mode="json") for b in ctx.bindings.list()]}

    # -- discovery ------------------------------------------------------
    @tool(annotations=read_only, full_only=True)
    async def list_engines() -> dict[str, Any]:
        """List engines with capabilities, install state and why_unavailable.

        Use it when choosing an `engine` argument, or when server_info() says an
        engine is unavailable and you need the reason.

        Read:
            engines[].engine_id: pass as `engine`.
            engines[].installed: false means it cannot run; see engines[].why_unavailable.
            engines[].streaming, engines[].pcm_native: whether incremental output is
                possible.
            engines[].models[]: aliases on that engine, each with a `loaded` flag.
        """
        entries = ctx.registry.list_entries(loaded=ctx.workers.loaded_models())
        return {"engines": [e.model_dump(mode="json") for e in entries]}

    @tool(annotations=read_only, full_only=True)
    async def list_models(engine_id: str | None = None) -> dict[str, Any]:
        """List models across engines with HF repo, download and loaded state.

        Use it before passing `model=` to speak(), or to see whether a download is
        needed for the quality tier you want.

        Read:
            models[].alias: pass as `model`, or as "engine_id:alias".
            models[].engine_id: which engine serves it.
            models[].mode: "clone" (needs a voice), "custom_voice" (preset speakers)
                or "voice_design" (design_voice()).
            models[].preset_speakers[]: names usable as speak(speaker=...).
            models[].quality_tier: "high" (1.7B) or "fast" (0.6B).
            models[].downloaded_locally: weights present in the local HF cache.
            models[].loaded: resident in VRAM right now.
        """
        loaded = ctx.workers.loaded_models()
        return {
            "models": [
                model.model_copy(
                    update={"loaded": model.alias in loaded.get(model.engine_id, [])}
                ).model_dump(mode="json")
                for model in ctx.registry.list_models(engine_id)
            ]
        }

    @tool(annotations=open_world_mutation, full_only=True)
    async def download_model(engine_id: str, model_alias: str) -> dict[str, Any]:
        """Start an async HuggingFace download of an already-registered model.

        Use it to warm the cache before a batch. Refused unless
        ALLOW_AGENT_ENGINE_MUTATION=true and both the daemon and the caller are
        loopback; register_model() is what adds a model that is not listed yet.

        Read:
            job_id: poll get_job(); its `result.downloaded_locally` confirms the
                weights actually landed in the HF cache.
            kind: always "download_model".
        """
        from .api.routes_engines import _run_download

        try:
            _require_mutation_allowed(settings, "download_model")
            ctx.registry.model_spec(engine_id, model_alias)
            job = ctx.jobs.create("download_model")
            ctx.jobs.register_task(
                job.job_id,
                asyncio.create_task(_run_download(ctx, engine_id, model_alias, job.job_id)),
            )
            return {"job_id": job.job_id, "kind": "download_model"}
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    @tool(annotations=open_world_mutation, full_only=True)
    async def register_model(
        engine_id: str,
        model_alias: str,
        hf_repo: str,
        quality_tier: QualityTier = "high",
        est_vram_gb: float = 4.4,
        download: bool = True,
    ) -> dict[str, Any]:
        """Add a model to an engine's manifest, hot-reload, optionally download it.

        Use it to point an engine at another HuggingFace repository. No daemon
        restart is needed. Refused unless ALLOW_AGENT_ENGINE_MUTATION=true and both
        the daemon and the caller are loopback: this rewrites engine.yaml on disk.

        Read:
            engine_id, model_alias: the new pair, now usable as `model`.
            job_id: the download job when `download` is true, else null.
        """
        from .api.routes_engines import _run_download

        try:
            _require_mutation_allowed(settings, "register_model")
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
                    asyncio.create_task(_run_download(ctx, engine_id, model_alias, job_id)),
                )
            return {
                "engine_id": engine_id,
                "model_alias": model_alias,
                "job_id": job_id,
            }
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    @tool(annotations=open_world_mutation, full_only=True)
    async def register_engine(
        engine_id: str,
        engine_yaml_content: str,
        pyproject_toml_content: str | None = None,
        worker_py_content: str | None = None,
        install_venv: bool = True,
    ) -> dict[str, Any]:
        """Scaffold a new engine plugin and hot-reload it, without a daemon restart.

        Use it only when a required engine is genuinely absent and you are the
        operator of this machine: `worker_py_content` is Python the daemon will
        execute. `engine_id` must be a lowercase slug; path traversal is rejected.
        Refused unless ALLOW_AGENT_ENGINE_MUTATION=true and both ends are loopback.

        With REQUIRE_ENGINE_APPROVAL=true the plugin is staged under
        engines/.pending/<engine_id>/ and NOT loaded — discovery ignores it, so
        nothing can run until an operator promotes it. Otherwise it is live at once.

        Read:
            engine_id: the id that was registered.
            staged: true when the plugin is waiting for operator approval.
            staged_path: where it was written (null when live).
            installed: whether it can run now; always false when staged.
            job_id: the venv install job when one was started, else null.
        """
        from .api.routes_engines import _run_install

        try:
            _require_mutation_allowed(settings, "register_engine")

            if settings.require_engine_approval:
                # Staging lives in the registry so the CLI promotes through the
                # exact same code path. Promotion is deliberately not reachable
                # from here: activating a staged plugin is the operator's call.
                try:
                    record = ctx.registry.register_engine(
                        engine_id=engine_id,
                        engine_yaml_content=engine_yaml_content,
                        pyproject_toml_content=pyproject_toml_content,
                        worker_py_content=worker_py_content,
                        staging=True,
                    )
                except ValueError as exc:
                    raise AgentVoiceError(
                        ErrorCode.INVALID_SLUG,
                        f"invalid engine id {engine_id!r}: {exc}",
                        "Use a lowercase slug: letters, digits and underscores, 2-41 chars.",
                    ) from exc
                return {
                    "engine_id": engine_id,
                    "staged": True,
                    "staged_path": str(record.directory),
                    "installed": False,
                    "job_id": None,
                }

            try:
                record = ctx.registry.register_engine(
                    engine_id=engine_id,
                    engine_yaml_content=engine_yaml_content,
                    pyproject_toml_content=pyproject_toml_content,
                    worker_py_content=worker_py_content,
                )
            except ValueError as exc:
                # A bad slug or manifest surfaces as a pydantic ValidationError,
                # which would otherwise escape as an unstructured tool exception.
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
                "staged": False,
                "staged_path": None,
                "installed": ctx.registry.get(engine_id).installed,
                "job_id": job_id,
            }
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    @tool(annotations=open_world_mutation, full_only=True)
    async def install_engine(engine_id: str) -> dict[str, Any]:
        """Run `uv sync` for an engine and verify the IPC handshake, as a job.

        Use it when an engine is present but reports ENGINE_NOT_INSTALLED or a
        stale backend version. Refused unless ALLOW_AGENT_ENGINE_MUTATION=true and
        both ends are loopback: it installs packages and then executes the result.

        Read:
            job_id: poll get_job(); `result.installed` is true only once the
                handshake succeeded.
            kind: always "install_engine".
        """
        from .api.routes_engines import _run_install

        try:
            _require_mutation_allowed(settings, "install_engine")
            ctx.registry.get(engine_id)
            job = ctx.jobs.create("install_engine")
            ctx.jobs.register_task(
                job.job_id,
                asyncio.create_task(_run_install(ctx, engine_id, job.job_id)),
            )
            return {"job_id": job.job_id, "kind": "install_engine"}
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    # -- GPU lifecycle --------------------------------------------------
    @tool(annotations=read_only)
    async def gpu_status() -> dict[str, Any]:
        """Report worker state, resident models, VRAM and the idle-unload countdown.

        Reads only: it never spawns or wakes a worker.
        """
        return await ctx.workers.gpu_status()

    @tool(annotations=destructive)
    async def unload_models(force: bool = False) -> dict[str, Any]:
        """Free the GPU now: kill workers and drop their loaded models.

        Refuses with WORKER_BUSY while jobs run unless force=true.
        """
        try:
            return await ctx.workers.unload_models(force=force)
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    # -- transcription and dubbing ---------------------------------------
    @tool(annotations=idempotent)
    async def transcribe(
        audio_path: str,
        language: str | None = None,
        engine: str | None = None,
        model: str | None = None,
        hotwords: list[str] | None = None,
        word_timestamps: bool = False,
        export_srt: bool = False,
    ) -> dict[str, Any]:
        """Transcribe an audio or video file to timed segments.

        Returns text, language, duration_sec, segments_count and a bounded segment
        preview; the full transcript is at transcript_json_path.
        """
        from .api.routing import resolve_asr_target
        from .services.transcribe_service import transcribe_media

        try:
            try:
                # The frozen request model is the one validator of a hotword
                # list; building it here means MCP cannot hand the decoder a
                # longer list than /v1/agent/transcribe would accept.
                payload = TranscribeRequest(
                    audio_path=audio_path,
                    language=language,
                    engine=engine,
                    model=model,
                    hotwords=hotwords,
                    word_timestamps=word_timestamps,
                    export_srt=export_srt,
                )
            except ValueError as exc:
                raise AgentVoiceError(
                    ErrorCode.BAD_REQUEST,
                    f"invalid transcribe request: {exc}",
                    f"At most {settings.max_hotwords} hotwords, each under 64 "
                    "characters.",
                ) from exc
            # The same confinement clone_voice uses, with a media noun: a video
            # that cannot be found must not be reported as a missing clip.
            media = resolve_media_path(
                payload.audio_path,
                inbox_dir=settings.inbox_dir,
                mcp_base_path=settings.mcp_base_path,
                allow_any_path=settings.allow_any_path,
                field="audio_path",
            )
            target = resolve_asr_target(
                settings=settings,
                registry=ctx.registry,
                engine=payload.engine,
                model=payload.model,
            )
            result = await transcribe_media(
                ctx,
                audio_path=media,
                target=target,
                language=payload.language,
                word_timestamps=payload.word_timestamps,
                hotwords=payload.hotwords,
                project=payload.project,
                export_srt=payload.export_srt,
                agent_id=_agent_id(),
            )
            return result.model_dump(mode="json")
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    @tool(annotations=idempotent)
    async def dub_prepare(
        media_path: str,
        source_language: str | None = None,
        asr_engine: str | None = None,
        asr_model: str | None = None,
        hotwords: list[str] | None = None,
        extract_speaker_ref: bool = True,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Stage a video for dubbing: transcribe it and extract a speaker reference.

        Returns dub_id and timed segments; translate them, then call dub_render.
        """
        from .services.dub_service import prepare_dub

        try:
            try:
                payload = DubPrepareRequest(
                    media_path=media_path,
                    source_language=source_language,
                    asr_engine=asr_engine,
                    asr_model=asr_model,
                    hotwords=hotwords,
                    extract_speaker_ref=extract_speaker_ref,
                    project=project,
                )
            except ValueError as exc:
                # An over-cap hotword list or a bad project slug is a caller
                # mistake; it must surface as the canonical JSON error body
                # rather than an unstructured tool exception.
                raise AgentVoiceError(
                    ErrorCode.BAD_REQUEST,
                    f"invalid dub prepare request: {exc}",
                    f"At most {settings.max_hotwords} hotwords, each under 64 "
                    "characters; project must be a lowercase slug.",
                ) from exc
            # Refuse a path escape before any ffmpeg probe or worker wake. The
            # service confines the same path under the same policy; this only
            # refuses it earlier, so the two cannot disagree.
            resolve_media_path(
                payload.media_path,
                inbox_dir=settings.inbox_dir,
                mcp_base_path=settings.mcp_base_path,
                allow_any_path=settings.allow_any_path,
                field="media_path",
            )
            return (await prepare_dub(ctx, payload)).model_dump(mode="json")
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    @tool(annotations=idempotent)
    async def dub_render(
        dub_id: str,
        segments: list[dict[str, Any]],
        voice: str | None = None,
        speaker: str | None = None,
        auto_clone_source: bool = False,
        engine: str | None = None,
        model: str | None = None,
        quality: QualityTier = "high",
        keep_original_bed: bool = True,
        bed_duck_db: float = -18.0,
        max_atempo: float = 1.35,
        export_srt: bool = True,
        burn_subtitles: bool = False,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Synthesize translated lines onto the timeline and mux the dubbed MP4.

        Read overflow_warnings: those lines still overran even at max_atempo.
        """
        from .services.dub_service import render_dub

        try:
            try:
                payload = DubRenderRequest(
                    dub_id=dub_id,
                    segments=[DubSegmentSpec(**segment) for segment in segments],
                    voice=voice,
                    speaker=speaker,
                    auto_clone_source=auto_clone_source,
                    engine=engine,
                    model=model,
                    quality=quality,
                    keep_original_bed=keep_original_bed,
                    bed_duck_db=bed_duck_db,
                    max_atempo=max_atempo,
                    export_srt=export_srt,
                    burn_subtitles=burn_subtitles,
                    project=project,
                )
            except ValueError as exc:
                raise AgentVoiceError(
                    ErrorCode.BAD_REQUEST,
                    f"invalid dub render request: {exc}",
                    "Each segment needs index, start_sec, end_sec and text; "
                    "bed_duck_db is -40..0 and max_atempo is 1.0..2.0.",
                ) from exc
            return (await render_dub(ctx, payload)).model_dump(mode="json")
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    @tool(annotations=ToolAnnotations(idempotent_hint=True, destructive_hint=False))
    async def start_dub_job(
        dub_id: str,
        segments: list[dict[str, Any]],
        voice: str | None = None,
        speaker: str | None = None,
        auto_clone_source: bool = False,
        engine: str | None = None,
        model: str | None = None,
        quality: QualityTier = "high",
        keep_original_bed: bool = True,
        bed_duck_db: float = -18.0,
        max_atempo: float = 1.35,
        export_srt: bool = True,
        burn_subtitles: bool = False,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Start dub_render as a background job; poll it with get_job(job_id)."""
        from .services.dub_service import render_dub

        try:
            try:
                payload = DubRenderRequest(
                    dub_id=dub_id,
                    segments=[DubSegmentSpec(**segment) for segment in segments],
                    voice=voice,
                    speaker=speaker,
                    auto_clone_source=auto_clone_source,
                    engine=engine,
                    model=model,
                    quality=quality,
                    keep_original_bed=keep_original_bed,
                    bed_duck_db=bed_duck_db,
                    max_atempo=max_atempo,
                    export_srt=export_srt,
                    burn_subtitles=burn_subtitles,
                    project=project,
                )
            except ValueError as exc:
                raise AgentVoiceError(
                    ErrorCode.BAD_REQUEST,
                    f"invalid dub render request: {exc}",
                    "Each segment needs index, start_sec, end_sec and text; "
                    "bed_duck_db is -40..0 and max_atempo is 1.0..2.0.",
                ) from exc
            job = ctx.jobs.create("dub")
            # No agent id is captured here, unlike start_script_job: render_dub
            # takes no caller identity (dubbing does not participate in voice
            # bindings), so there is nothing request-scoped to carry over.
            async def runner() -> None:
                ctx.jobs.mark_running(job.job_id)
                try:
                    # Progress per translated line, not per job: a hundred-line
                    # dub that jumps 0.01 -> 1.0 tells a polling agent nothing.
                    result = await render_dub(
                        ctx,
                        payload,
                        on_progress=lambda done, total, message: ctx.jobs.update(
                            job.job_id,
                            progress=(done / total if total else 1.0),
                            message=message,
                        ),
                    )
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
            return {"job_id": job.job_id, "kind": "dub"}
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    # -- resources ------------------------------------------------------
    @mcp.resource("agent-voice://voices", name="voices", mime_type="application/json")
    async def voices_resource() -> str:
        """Every registered voice as JSON. Read-only; never wakes the GPU."""
        return json.dumps(
            [voice.model_dump(mode="json") for voice in ctx.voices.list()], indent=2
        )

    @mcp.resource("agent-voice://guide", name="guide", mime_type="text/markdown")
    async def guide_resource() -> str:
        """The agent-voice usage guide, so an agent can read it without a repo path."""
        return _guide_text()

    # -- prompt ---------------------------------------------------------
    @mcp.prompt(name="narrate_video")
    def narrate_video(topic: str, beat_count: int = 5) -> str:
        """Plan and render a timed voice-over for a video, start to finish."""
        return (
            f"Produce a timed voice-over about {topic}, in {beat_count} beats.\n"
            "1. Call server_info() first. It is free and tells you the default "
            "voice, voices_count, and which engines are installed.\n"
            "2. If voices_count == 0 you cannot speak yet: call "
            "clone_voice(name, ref_audio_path, ref_text) with 3-10 s of clean "
            "single-speaker speech and an accurate transcript, then continue.\n"
            f"3. Draft exactly {beat_count} beats of narration about {topic}. One "
            "or two sentences each, meant to be spoken aloud in order; no headings "
            "or bullets inside a beat. Give each beat its text and, where a visual "
            "needs room to land, pause_after_sec (0.2-1.5).\n"
            "4. Render with speak_script(beats=[{text, pause_after_sec}], "
            "pause_after_sec=0.35, export_srt=True). For a long script use "
            "start_script_job(...) instead and poll get_job(job_id).\n"
            "5. Read the result: file_path is the master audio, beats[].start_sec / "
            "end_sec / duration_sec are the timings to cut visuals against, "
            "srt_path and vtt_path hold subtitles, and timing_json_path is the same "
            "data on disk. Report those paths; never inline audio.\n"
        )

    @mcp.prompt(name="dub_video")
    def dub_video(media_path: str, target_language: str) -> str:
        """Dub a video into another language, start to finish."""
        return (
            f"Dub {media_path} into {target_language}.\n"
            "1. Call server_info() first. It is free and tells you "
            "dubbing_available, default_asr_engine and asr_engines.\n"
            f"2. Call dub_prepare(media_path='{media_path}') to transcribe the "
            "source and extract a speaker reference. Read dub_id and segments[] "
            "(index, start_sec, end_sec, text).\n"
            f"3. Translate every segment into {target_language}, then TIGHTEN each "
            "line until it fits end_sec - start_sec: drop filler words and prefer "
            "shorter synonyms. Keep index, start_sec and end_sec exactly as "
            "returned; those are the slots on the source timeline.\n"
            "4. Call dub_render(dub_id=..., segments=[{index, start_sec, end_sec, "
            "text}], auto_clone_source=True) so the dub keeps the original "
            "speaker's timbre. For a long file use start_dub_job(...) instead and "
            "poll get_job(job_id).\n"
            "5. Read overflow_warnings[]. Those lines still overran their slot at "
            "max_atempo. Shorten only those segments' text and render again with "
            "the same dub_id; the unchanged lines come back cached.\n"
            "6. Report file_path (the dubbed MP4), srt_path and vtt_path. Call "
            "unload_models() when you are done. Never inline video or audio bytes.\n"
        )

    return mcp
