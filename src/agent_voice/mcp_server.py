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

import yaml
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers, get_http_request
from mcp_types import ToolAnnotations
from pydantic import ValidationError

from .schemas import (
    AgentVoiceError,
    BeatSpec,
    EngineManifest,
    ErrorCode,
    ErrorResponse,
    QualityTier,
    ResponseFormat,
    ScriptRequest,
    ScriptResult,
    SpeakRequest,
    SynthesisResult,
    VoiceListResponse,
)
from .security import is_loopback_client, validate_slug
from .services.script_service import render_script
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


def _stage_engine(
    settings: SettingsLike,
    *,
    engine_id: str,
    engine_yaml_content: str,
    pyproject_toml_content: str | None,
    worker_py_content: str | None,
) -> Path:
    """Write a plugin to ``engines/.pending/<engine_id>/`` for operator approval.

    The validation ``EngineRegistry.register_engine`` performs is repeated here —
    slug, YAML mapping, and ``EngineManifest`` (which forbids unknown keys) —
    because a staged plugin is promoted verbatim. Promotion itself belongs to the
    registry/CLI: discovery only reads ``engines/<id>/engine.yaml``, so a staged
    directory cannot be loaded or spawned until an operator activates it.
    """
    try:
        validate_slug(engine_id)
    except ValueError as exc:
        raise AgentVoiceError(
            ErrorCode.INVALID_SLUG,
            f"invalid engine id {engine_id!r}: {exc}",
            "Use a lowercase slug: letters, digits and underscores, 2-41 chars.",
        ) from exc

    try:
        raw = yaml.safe_load(engine_yaml_content) or {}
    except yaml.YAMLError as exc:
        raise AgentVoiceError(
            ErrorCode.MANIFEST_INVALID,
            f"engine.yaml is not valid YAML: {exc}",
            "Fix the YAML and retry.",
        ) from exc
    if not isinstance(raw, dict):
        raise AgentVoiceError(
            ErrorCode.MANIFEST_INVALID,
            "engine.yaml must be a YAML mapping",
            "See engines/ADDING_AN_ENGINE.md for the manifest schema.",
        )

    raw["engine_id"] = engine_id
    try:
        EngineManifest(**raw)
    except ValidationError as exc:
        raise AgentVoiceError(
            ErrorCode.MANIFEST_INVALID,
            f"engine manifest rejected: {exc.error_count()} problem(s)",
            f"{exc}",
        ) from exc

    directory = Path(settings.engines_dir) / ".pending" / engine_id
    if directory.exists():
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"a pending plugin already exists for '{engine_id}'",
            "Ask the operator to approve or discard it before registering again.",
        )

    directory.mkdir(parents=True)
    (directory / "engine.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), "utf-8"
    )
    if pyproject_toml_content:
        (directory / "pyproject.toml").write_text(pyproject_toml_content, "utf-8")
    if worker_py_content:
        (directory / "worker.py").write_text(worker_py_content, "utf-8")
    return directory


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
        "content with timings. Tools return file paths, never audio blobs. "
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
        """Call this first: a free, side-effect-free snapshot of the whole server.

        Use it whenever you do not yet know what this machine can do. It never
        spawns a worker and never touches the GPU, so it costs nothing to call.

        Read:
            version: daemon version.
            default_engine, default_voice: what speak() uses when you pass neither.
            voices_count: 0 means you must clone_voice() before you can speak.
            voices[]: voice_id, name, duration_sec for every registered voice.
            installed_engines[]: ids of engines that can run right now.
            engines[]: engine_id, installed, streaming, why_unavailable — the last
                is the concrete reason an engine cannot be used.
            gpu_status: the same snapshot gpu_status() returns (see its notes).
        """
        from . import __version__

        entries = ctx.registry.list_entries(loaded=ctx.workers.loaded_models())
        voices = ctx.voices.list()
        default_voice = settings.default_voice_id or None
        if default_voice is None and len(voices) == 1:
            # A single registered voice is unambiguous: use it rather than
            # making the caller discover and pass its id.
            default_voice = voices[0].voice_id
        return {
            "version": __version__,
            "default_engine": settings.default_engine,
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
    @tool(annotations=idempotent)
    async def speak(
        text: str,
        voice: str | None = None,
        engine: str | None = None,
        model: str | None = None,
        quality: QualityTier = "high",
        language: str = "Auto",
        format: ResponseFormat = "wav",
        seed: int | None = None,
        speed: float = 1.0,
        take: int | None = None,
    ) -> SynthesisResult:
        """Synthesise ONE utterance to a file. For several beats use speak_script().

        Use it for a single line: no timing metadata, no stitching, cheapest path
        to audio. Repeating the same call is free (deterministic disk cache).

        Args:
            text: What to say.
            voice: Voice id or name. Defaults to the agent binding, then the server default.
            engine: Engine id, e.g. qwen3_tts_fast. Defaults to the configured engine.
            model: Model alias, or `engine:model`. Overrides `quality`.
            quality: "high" (1.7B, better) or "fast" (0.6B, quicker and smaller).
            language: "Auto" or a language name such as "English".
            format: wav, mp3, opus, flac or pcm.
            seed: Fixed seed: the same inputs then produce byte-identical audio.
            speed: Playback-rate multiplier applied by the engine.
            take: Alternate delivery of the same line. take=2 derives a repeatable
                new seed, so you can audition a different read of one line without
                losing the canonical one. Omit for the canonical delivery.

        Read:
            file_path: absolute local path; read this. Audio is never returned inline.
            download_url: HTTP URL for the same artifact, if you would rather hand a
                URL to another process than copy bytes yourself.
            duration_sec, format, engine_id, model_alias, voice_id: what was produced.
            cached: true when the deterministic disk cache answered. The audio is
                byte-identical to the original render and nothing touched the GPU,
                so a repeat call is free — do not pre-render out of fear of cost.
            chunks_used: >1 means the text exceeded the chunk limit and was
                synthesised in pieces and stitched. Normal, not an error.
            elapsed_ms: Wall-clock cost. Near zero on a cache hit, which makes a
                latency regression obvious to a caller.
            take: which alternate delivery this file is; null for the canonical take.
            real_time_factor: duration_sec / elapsed_sec. Above 1.0 is faster than
                real time, so 3.0 renders 30 s of speech in 10 s; null for streamed
                output.
        """
        from .api.routing import resolve_target

        try:
            request = SpeakRequest(
                text=text,
                voice=voice,
                engine=engine,
                model=model,
                quality=quality,
                language=language,
                format=format,
                seed=seed,
                speed=speed,
                take=take,
            )
            if len(request.text) > settings.max_text_chars:
                raise AgentVoiceError(
                    ErrorCode.TEXT_TOO_LONG,
                    f"text is {len(request.text)} characters, limit is "
                    f"{settings.max_text_chars}",
                    "Split it up or use speak_script.",
                )
            target = resolve_target(
                settings=settings,
                registry=ctx.registry,
                voices=ctx.voices,
                bindings=ctx.bindings,
                agent_id=_agent_id(),
                voice=request.voice,
                engine=request.engine,
                model=request.model,
                quality=request.quality,
            )
            return await ctx.workers.synthesize(
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
                take=request.take,
            )
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
    ) -> ScriptResult:
        """Synthesise a multi-beat script into one stitched file, with exact timings.

        Use it for narration with pauses, multi-speaker content, or anything whose
        timing another process has to match (video, slides, subtitles). For a
        single line, speak() is cheaper.

        Each beat is a BeatSpec (everything optional except `text`):
        {text, voice?, engine?, model?, quality?, language?, target_duration_sec?,
         fit_mode?, pause_after_sec?, take?}

        * `pause_after_sec` (0..10) inserts real silence after the beat and is
          included in the reported timings, so subtitles and downstream visuals
          stay frame-accurate. This is how you time a visual to land. The
          call-level `pause_after_sec` argument is a default for every beat that
          does not set its own; a beat's own value always wins.
        * `voice` per beat enables multi-speaker scripts (podcasts, interviews):
          each beat may name a different registered voice.
        * `target_duration_sec` + `fit_mode` ("none"|"pad"|"atempo") force a beat
          into a fixed slot on a video timeline.
        * `take` re-records one beat without disturbing the others.

        Read:
            file_path: the stitched master audio; read this.
            duration_sec: total length, pauses included.
            beats[]: index, text, start_sec, end_sec, duration_sec, pause_after_sec,
                voice_id (who actually spoke), target_duration_sec, fit_mode —
                everything a timeline or subtitle pass needs.
            beats[].cached: true when this beat was reused from disk. Re-rendering
                an edited script should show every untouched beat as a hit, so this
                tells you what the edit actually cost.
            beats[].artifact_id: the per-beat artifact id, so you can fetch or
                replace exactly one beat.
            srt_path, vtt_path: subtitle files, written when export_srt is true.
            timing_json_path: this whole result, written next to the audio.
        """
        try:
            payload = ScriptRequest(
                beats=_with_default_pause(beats, pause_after_sec),
                default_voice=default_voice,
                engine=engine,
                quality=quality,
                format=format,
                export_srt=export_srt,
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
    ) -> dict[str, Any]:
        """Start a script render in the background and return a job id immediately.

        Use it when a script is long enough that blocking the tool call would time
        out or stall your loop; each call creates a new job, so this is not
        idempotent — reuse the job_id instead of re-submitting. Takes exactly the
        same `beats` and the same call-level `pause_after_sec` default as
        speak_script (see BeatSpec for per-beat `pause_after_sec`, `voice`,
        `fit_mode` and `take`).

        Read:
            job_id: pass it to get_job(). Progress advances beat by beat, and the
                finished job's `result` is exactly the ScriptResult speak_script()
                would have returned.
            kind: always "script".
        """
        try:
            payload = ScriptRequest(
                beats=_with_default_pause(beats, pause_after_sec),
                default_voice=default_voice,
                engine=engine,
                quality=quality,
                format=format,
                export_srt=export_srt,
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
        """Poll a job started by start_script_job, download_model or install_engine.

        Call it in a short loop (with a sleep) until `status` leaves
        pending/running.

        Read:
            status: pending | running | succeeded | failed | cancelled.
            progress: 0..1; advances per beat for scripts.
            message: human-readable current step.
            result: the finished payload. For kind "script" it is exactly the
                ScriptResult speak_script() would have returned.
            error: {error_code, message, suggestion} when status is failed.
        """
        try:
            return ctx.jobs.get(job_id).model_dump(mode="json")
        except AgentVoiceError as exc:
            _raise_tool_error(exc)

    @tool(annotations=destructive)
    async def cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel a running job. A job that already finished is returned unchanged.

        Use it when you no longer need the result and want the GPU back sooner.

        Read:
            status: "cancelled" once the request took effect; a job that had
                already finished keeps its terminal status.
        """
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

        Use it when list_voices() is empty, or when you need another speaker. The
        clip is normalised to 24 kHz mono and silence-trimmed; 3-10 seconds of
        clean single-speaker speech is the quality sweet spot. Providing
        `ref_text` (an accurate transcript) makes the engine condition on both the
        audio and its words (ICL mode), which is audibly better than audio-only
        `x_vector_only=true`. A wrong transcript is worse than none.

        Idempotent: the same `name` plus byte-identical audio returns the existing
        voice instead of creating a duplicate, so a retried call is safe. If the
        name exists but the audio or transcript differs, the call fails rather
        than silently swapping a voice another job may be using — pass
        overwrite=true to replace it deliberately.

        Read:
            voice_id: pass this as `voice` to speak()/speak_script().
            duration_sec: how much usable speech the clip contributed.
            warnings[]: concrete quality problems (too short, clipped, silent).
        """
        from .security import ensure_within_base

        try:
            base = settings.mcp_base_path
            source = (
                ensure_within_base(ref_audio_path, base)
                if base
                else Path(ref_audio_path).expanduser()
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

    @tool(annotations=read_only)
    async def list_voices() -> VoiceListResponse:
        """List every registered voice. Use it when a voice id or name is unknown.

        Read:
            voices[].voice_id: pass as `voice`; voices[].name is the human label.
            voices[].duration_sec: reference length; very short clips clone worse.
            voices[].warnings[]: what to fix about the reference audio.
            voices[].ref_text, voices[].x_vector_only_mode: how the voice was
                registered, which explains a quality difference between two voices.
        """
        return VoiceListResponse(voices=ctx.voices.list())

    @tool(annotations=destructive, full_only=True)
    async def delete_voice(voice_id: str) -> dict[str, str]:
        """Delete a voice and all of its cached prompts. This cannot be undone.

        Use it to reclaim disk, or to replace bad reference audio: delete first,
        then clone_voice() again with the same name.

        Read:
            deleted: the voice_id that was removed.
        """
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
                # Promotion is deliberately not implemented here: activating a
                # staged plugin is the operator's decision (registry/CLI), and
                # doing it from an agent would defeat the approval gate.
                directory = _stage_engine(
                    settings,
                    engine_id=engine_id,
                    engine_yaml_content=engine_yaml_content,
                    pyproject_toml_content=pyproject_toml_content,
                    worker_py_content=worker_py_content,
                )
                return {
                    "engine_id": engine_id,
                    "staged": True,
                    "staged_path": str(directory),
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

        Use it to decide whether the next call is warm, and to see what is holding
        the GPU. Reads only: it never spawns or wakes a worker.

        Read:
            workers: engine_id -> alive|dead. A dead entry means the next request
                has to respawn the worker and reload weights.
            loaded_models: "engine:alias" entries currently resident in VRAM.
            vram_used_mb, vram_free_mb: whole-GPU figures; null without NVML.
            queue_depth, in_flight: jobs waiting for the engine lock, jobs running.
            unload_in_seconds: seconds until the idle reaper unloads the resident
                model (MODEL_IDLE_TTL_SECONDS minus idle time). null when nothing
                is resident, or when work is queued/running so the countdown is
                paused. Any request resets it, so a small value means the model is
                about to be dropped and your next call will be a cold start.
            last_used_at: unix timestamp of the last synthesis.
        """
        return await ctx.workers.gpu_status()

    @tool(annotations=destructive)
    async def unload_models(force: bool = False) -> dict[str, Any]:
        """Free the GPU now: kill workers and drop their loaded models.

        Use it when another GPU application needs the VRAM, or after a large batch
        you no longer need. Both workers and their models go — the next synthesis
        pays a cold start. Without `force` it refuses with WORKER_BUSY while jobs
        are running; force=true cancels them and frees the memory immediately.

        Read:
            unloaded: engine ids whose workers were killed.
            cancelled_jobs: jobs cancelled by force=true.
        """
        try:
            return await ctx.workers.unload_models(force=force)
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

    return mcp
