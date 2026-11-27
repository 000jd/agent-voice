"""`agent-voice` command line interface.

Every command is import-light: torch lives only in the worker processes, so a
`cache stats` or `engines pending` call never touches CUDA. Engine/model/voice
imports are deferred into the command bodies for the same reason.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import ValidationError
from pydantic_settings import SettingsError

from .config import Settings, get_settings

# Imported eagerly, unlike the engine/model/voice modules below: Typer evaluates
# option defaults when this module is imported, so the `voices design` sample text
# has to be available here. `schemas` is torch-free by contract (see its module
# docstring), so this does not break the "no torch in the CLI" rule.
from .schemas import DEFAULT_DESIGN_SAMPLE_TEXT

app = typer.Typer(
    help="Headless multi-engine voice server for AI agents.", no_args_is_help=True
)
engines_app = typer.Typer(help="Inspect, install and test engines.", no_args_is_help=True)
models_app = typer.Typer(help="Inspect and download models.", no_args_is_help=True)
voices_app = typer.Typer(help="Manage reference voices.", no_args_is_help=True)
bindings_app = typer.Typer(help="Manage per-agent bindings.", no_args_is_help=True)
cache_app = typer.Typer(help="Inspect and clear the synthesis cache.", no_args_is_help=True)
pronunciations_app = typer.Typer(
    help="Manage the persistent pronunciation lexicon.", no_args_is_help=True
)
dub_app = typer.Typer(
    help="Two-stage video dubbing: prepare here, translate in the agent, render here.",
    no_args_is_help=True,
)

app.add_typer(engines_app, name="engines")
app.add_typer(models_app, name="models")
app.add_typer(voices_app, name="voices")
app.add_typer(bindings_app, name="bindings")
app.add_typer(cache_app, name="cache")
app.add_typer(pronunciations_app, name="pronunciations")
app.add_typer(dub_app, name="dub")


def _print(payload) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


def _human_bytes(count: float) -> str:
    """Binary-prefixed size, so a 5 GiB cap never renders as 5.4 GB."""
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(value) < 1024.0:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def _iso(timestamp: float) -> str:
    """UTC ISO-8601 for a POSIX timestamp; epoch 0 means 'unknown'."""
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


# ---------------------------------------------------------------------------
# serve / mcp
# ---------------------------------------------------------------------------


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Bind address (default 127.0.0.1)."),
    port: int | None = typer.Option(None, help="Bind port (default 8000)."),
    reload: bool = typer.Option(False, help="Auto-reload on code change (development)."),
) -> None:
    """Run the daemon. Holds no GPU memory until a synthesis request arrives."""
    import uvicorn

    from .api.app import _refuse_unsafe_bind
    from .schemas import AgentVoiceError

    settings = get_settings()

    # `--host`/`--port` beat the settings file, so the safety check has to judge
    # the address uvicorn is actually about to bind, not the configured one: an
    # operator running `serve --host 0.0.0.0` on a loopback-configured checkout
    # must not get an unauthenticated server on the LAN. `is not None`, not `or`:
    # `--port 0` means "any free port" and must survive to uvicorn.
    effective_host = host if host is not None else settings.host
    effective_port = port if port is not None else settings.port
    try:
        _refuse_unsafe_bind(
            settings.model_copy(update={"host": effective_host, "port": effective_port})
        )
    except AgentVoiceError as exc:
        # One line and a suggestion, never a traceback: this is a configuration
        # refusal, and the operator has to read the fix.
        typer.secho(exc.message, fg=typer.colors.RED, err=True)
        if exc.suggestion:
            typer.echo(exc.suggestion, err=True)
        raise typer.Exit(code=1) from exc

    # Only now touch the filesystem: a refused bind leaves no pid file behind.
    settings.ensure_dirs()

    pid_file = settings.data_dir / "daemon.pid"
    pid_file.write_text(str(os.getpid()), "utf-8")

    uvicorn.run(
        "agent_voice.api.app:create_app",
        factory=True,
        host=effective_host,
        port=effective_port,
        reload=reload,
        log_level="info",
    )


@app.command()
def mcp() -> None:
    """Run the stdio MCP shim (starts the daemon if needed)."""
    from .mcp_stdio_proxy import run_stdio_proxy
    from .schemas import AgentVoiceError

    try:
        run_stdio_proxy()
    except AgentVoiceError as exc:
        typer.secho(
            f"{exc.error_code}: {exc.message}", fg=typer.colors.RED, err=True
        )
        if exc.suggestion:
            typer.echo(exc.suggestion, err=True)
        raise typer.Exit(code=1) from exc


# ---------------------------------------------------------------------------
# status / unload
# ---------------------------------------------------------------------------


@app.command()
def status() -> None:
    """Show daemon health, engines, and what is resident on the GPU."""
    import httpx

    settings = get_settings()
    try:
        health = httpx.get(f"{settings.base_url}/health", timeout=3.0).json()
    except httpx.HTTPError:
        typer.secho(f"daemon not reachable at {settings.base_url}", fg=typer.colors.YELLOW)
        typer.echo("Start it with: agent-voice serve")
        raise typer.Exit(code=1) from None

    typer.echo(f"daemon: {settings.base_url}  version {health.get('version')}")
    typer.echo(f"torch loaded in core: {health.get('torch_loaded')}")

    try:
        gpu = httpx.get(f"{settings.base_url}/v1/gpu", timeout=5.0).json()
        _print(gpu)
    except httpx.HTTPError as exc:
        typer.echo(f"(gpu status unavailable: {exc})")


@app.command()
def unload(
    force: bool = typer.Option(
        False, "--force", help="Cancel running jobs and free VRAM immediately."
    )
) -> None:
    """Unload all models, returning the GPU to 0 MB when it is idle."""
    import httpx

    settings = get_settings()
    try:
        response = httpx.post(
            f"{settings.base_url}/v1/models/unload",
            params={"force": str(force).lower()},
            timeout=60.0,
        )
    except httpx.HTTPError as exc:
        typer.secho(f"daemon not reachable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if response.status_code == 409:
        body = response.json()
        typer.secho(f"{body['error_code']}: {body['message']}", fg=typer.colors.YELLOW)
        typer.echo(body.get("suggestion", ""))
        raise typer.Exit(code=1)
    response.raise_for_status()
    _print(response.json())


# ---------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------


@app.command()
def transcribe(
    audio_or_video: Path = typer.Argument(
        ...,
        metavar="AUDIO_OR_VIDEO",
        help="File to transcribe; confined to the inbox on an agent-shaped host.",
    ),
    engine: str | None = typer.Option(
        None, "--engine", help="ASR engine id (default: AGENT_VOICE_DEFAULT_ASR_ENGINE)."
    ),
    model: str | None = typer.Option(
        None, "--model", help="ASR model alias, e.g. phonon-2.5, or engine:alias."
    ),
    language: str | None = typer.Option(
        None, "--language", help="Language hint; omit to let the backend detect it."
    ),
    hotwords: str = typer.Option(
        "",
        "--hotwords",
        help="Comma-separated bias terms ('CUDA,Manim'), at most 25.",
    ),
    words: bool = typer.Option(
        False, "--words", help="Include per-word start/end timestamps in segments."
    ),
    srt: bool = typer.Option(
        False, "--srt", help="Also write .srt and .vtt sidecars next to the transcript."
    ),
    project: str | None = typer.Option(
        None, "--project", help="Scoped-lexicon project whose terms become hotwords."
    ),
) -> None:
    """Transcribe a local audio or video file, in-process, with no daemon.

    Runs the same service the REST/MCP surfaces call, so the result is identical:
    a `TranscribeResult` on stdout plus a one-line summary. The path goes through
    `resolve_media_path`, the same inbox/MCP_BASE_PATH resolver an agent hits, so a
    run that works here works there.
    """
    import asyncio

    from .api.app import AppContext
    from .api.routing import resolve_asr_target
    from .schemas import AgentVoiceError, TranscribeRequest
    from .security import resolve_media_path
    from .services.transcribe_service import transcribe_media

    settings = get_settings()
    hotword_list = [term.strip() for term in hotwords.split(",") if term.strip()]
    try:
        # `absolute()` (not `resolve()`) so the operator's shell-relative path is
        # anchored where they typed it before the resolver judges confinement.
        # A relative path handed straight to `resolve_media_path` would be read
        # against the inbox base, which is right for an agent but surprising at a
        # prompt: `agent-voice transcribe data/inbox/clip.wav` must just work.
        media = resolve_media_path(
            str(Path(audio_or_video).expanduser().absolute()),
            inbox_dir=settings.inbox_dir,
            mcp_base_path=settings.mcp_base_path,
            allow_any_path=settings.allow_any_path,
        )
        ctx = AppContext(settings)
        target = resolve_asr_target(
            settings=settings, registry=ctx.registry, engine=engine, model=model
        )
        # The schema is the admission gate: it validates the hotword cap and the
        # project slug before any ffmpeg or worker work happens.
        payload = TranscribeRequest(
            audio_path=str(media),
            engine=target.engine_id,
            model=target.model_alias,
            language=language,
            hotwords=hotword_list or None,
            word_timestamps=words,
            export_srt=srt,
            project=project,
        )
    except AgentVoiceError as exc:
        typer.secho(f"{exc.error_code}: {exc.message}", fg=typer.colors.RED, err=True)
        if exc.suggestion:
            typer.echo(exc.suggestion, err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        # A pydantic validation error from the request schema, one line only.
        typer.secho(f"BAD_REQUEST: {_one_line(exc)}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    async def run():
        await ctx.workers.start()
        try:
            return await transcribe_media(
                ctx,
                audio_path=media,
                target=target,
                language=payload.language,
                word_timestamps=payload.word_timestamps,
                hotwords=payload.hotwords,
                project=payload.project,
                export_srt=payload.export_srt,
                agent_id=None,
            )
        finally:
            await ctx.workers.stop()

    try:
        result = asyncio.run(run())
    except AgentVoiceError as exc:
        typer.secho(f"{exc.error_code}: {exc.message}", fg=typer.colors.RED, err=True)
        if exc.suggestion:
            typer.echo(exc.suggestion, err=True)
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt:
        typer.secho("interrupted", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=130) from None

    _print(result.model_dump(mode="json"))
    speed = (
        f", {result.realtime_multiple:.1f}x realtime"
        if result.realtime_multiple
        else ""
    )
    typer.secho(
        f"{result.segments_count} segments, {result.duration_sec:.1f} s audio"
        f"{speed} -> {result.transcript_json_path or result.engine_id}",
        fg=typer.colors.GREEN,
    )


# ---------------------------------------------------------------------------
# dub
# ---------------------------------------------------------------------------


def _load_dub_segments(path: Path) -> list:
    """Parse a translated-lines file: a bare JSON array or `{"segments": [...]}`.

    Both shapes show up in practice: an agent that just translated the list writes
    the array, while an agent that round-trips `prepare`'s result writes the
    object. Accepting both avoids a pointless edit step on either side.
    """
    from .schemas import AgentVoiceError, DubSegmentSpec, ErrorCode

    try:
        raw = json.loads(path.read_text("utf-8"))
    except OSError as exc:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"cannot read segments file: {path} ({_one_line(exc)})",
            "Pass a path to a JSON file you can read.",
        ) from exc
    except json.JSONDecodeError as exc:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"segments file is not valid JSON: {exc}",
            'Write a JSON array of segments, or an object with a "segments" key.',
        ) from exc

    if isinstance(raw, dict):
        raw = raw.get("segments")
    if not isinstance(raw, list) or not raw:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"segments file must contain a non-empty list: {path}",
            'Write a JSON array of {index, start_sec, end_sec, text}, or an object '
            'with a "segments" key holding one.',
        )
    try:
        return [DubSegmentSpec(**entry) for entry in raw]
    except (TypeError, ValueError) as exc:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"segments file has an invalid entry: {_one_line(exc)}",
            "Each segment needs index, start_sec, end_sec and text; the rest is optional.",
        ) from exc


@dub_app.command("prepare")
def dub_prepare(
    media: Path = typer.Argument(..., metavar="MEDIA", help="Source video or audio to dub."),
    asr_engine: str | None = typer.Option(
        None, "--asr-engine", help="ASR engine id (default: the configured ASR engine)."
    ),
    asr_model: str | None = typer.Option(
        None, "--asr-model", help="ASR model alias, or engine:alias."
    ),
    language: str | None = typer.Option(
        None, "--language", help="Source-language hint; omit to auto-detect."
    ),
    hotwords: str = typer.Option(
        "", "--hotwords", help="Comma-separated bias terms ('CUDA,Manim'), at most 25."
    ),
    speaker_ref: bool = typer.Option(
        True,
        "--speaker-ref/--no-speaker-ref",
        help="Extract a speaker reference clip, so a render can clone the source voice.",
    ),
    project: str | None = typer.Option(
        None, "--project", help="Scoped-lexicon project whose terms become hotwords."
    ),
) -> None:
    """Stage 1 of dubbing: transcribe, extract the bed and a speaker reference.

    Writes `<DATA_DIR>/dubs/<dub_id>/project.json` and returns the timed source
    segments. Translation happens in *your* context — there is no bundled
    translation model — then `dub render` speaks the translated lines.
    """
    import asyncio

    from .api.app import AppContext
    from .schemas import AgentVoiceError, DubPrepareRequest
    from .security import resolve_media_path
    from .services.dub_service import prepare_dub

    settings = get_settings()
    hotword_list = [term.strip() for term in hotwords.split(",") if term.strip()]
    try:
        # Anchored the same way as `transcribe`: a CLI path is relative to the
        # operator's cwd, while the resolver's own base is the inbox.
        media_path = resolve_media_path(
            str(Path(media).expanduser().absolute()),
            inbox_dir=settings.inbox_dir,
            mcp_base_path=settings.mcp_base_path,
            allow_any_path=settings.allow_any_path,
        )
        payload = DubPrepareRequest(
            media_path=str(media_path),
            source_language=language,
            asr_engine=asr_engine,
            asr_model=asr_model,
            hotwords=hotword_list or None,
            extract_speaker_ref=speaker_ref,
            project=project,
        )
    except AgentVoiceError as exc:
        typer.secho(f"{exc.error_code}: {exc.message}", fg=typer.colors.RED, err=True)
        if exc.suggestion:
            typer.echo(exc.suggestion, err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.secho(f"BAD_REQUEST: {_one_line(exc)}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    ctx = AppContext(settings)

    async def run():
        await ctx.workers.start()
        try:
            return await prepare_dub(ctx, payload)
        finally:
            await ctx.workers.stop()

    try:
        result = asyncio.run(run())
    except AgentVoiceError as exc:
        typer.secho(f"{exc.error_code}: {exc.message}", fg=typer.colors.RED, err=True)
        if exc.suggestion:
            typer.echo(exc.suggestion, err=True)
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt:
        typer.secho("interrupted", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=130) from None

    _print(result.model_dump(mode="json"))
    typer.secho(
        f"dub {result.dub_id}: {len(result.segments)} segments, "
        f"speaker ref {result.speaker_ref_path or '(none)'}",
        fg=typer.colors.GREEN,
    )


@dub_app.command("render")
def dub_render(
    dub_id: str = typer.Argument(..., metavar="DUB_ID", help="The id `dub prepare` returned."),
    segments_json: Path = typer.Argument(
        ...,
        metavar="SEGMENTS_JSON",
        help='Translated lines: a JSON array, or an object with a "segments" key.',
    ),
    voice: str | None = typer.Option(
        None, "--voice", help="Registered voice id or name to speak the lines."
    ),
    speaker: str | None = typer.Option(
        None, "--speaker", help="Preset timbre instead of a cloned voice."
    ),
    auto_clone_source: bool = typer.Option(
        False,
        "--auto-clone-source",
        help="Clone the source speaker from the prepared reference clip.",
    ),
    engine: str | None = typer.Option(None, "--engine", help="TTS engine id."),
    model: str | None = typer.Option(None, "--model", help="TTS model alias."),
    quality: Literal["high", "fast"] = typer.Option(
        "high", "--quality", help="high | fast, when --model is left unset."
    ),
    keep_bed: bool = typer.Option(
        True,
        "--bed/--no-bed",
        help="Keep the original audio bed, ducked under the voice (default), or replace it.",
    ),
    bed_duck_db: float = typer.Option(
        -18.0, "--bed-duck-db", min=-40.0, max=0.0, help="Duck depth for the original bed."
    ),
    max_atempo: float = typer.Option(
        1.35,
        "--max-atempo",
        min=1.0,
        max=2.0,
        help="Hard ceiling on speed-up; an over-long line is reported, never cut.",
    ),
    srt: bool = typer.Option(
        True, "--srt/--no-srt", help="Write .srt and .vtt from the translated segments."
    ),
    burn_subtitles: bool = typer.Option(
        False, "--burn-subtitles", help="Burn the subtitles into the video (re-encodes)."
    ),
    project: str | None = typer.Option(
        None, "--project", help="Scoped-lexicon project for the spoken lines."
    ),
) -> None:
    """Stage 2 of dubbing: speak the translated lines into their original slots.

    Each line is synthesised through the ordinary voice path (cache, markup,
    lexicon), retimed into its slot up to `--max-atempo`, placed on a timeline and
    mixed with the original bed. A line that still overruns is reported in
    `overflow_warnings` — never truncated.
    """
    import asyncio

    from .api.app import AppContext
    from .schemas import AgentVoiceError, DubRenderRequest
    from .services.dub_service import render_dub

    settings = get_settings()
    try:
        segments = _load_dub_segments(segments_json)
        payload = DubRenderRequest(
            dub_id=dub_id,
            segments=segments,
            voice=voice,
            speaker=speaker,
            auto_clone_source=auto_clone_source,
            engine=engine,
            model=model,
            quality=quality,
            keep_original_bed=keep_bed,
            bed_duck_db=bed_duck_db,
            max_atempo=max_atempo,
            export_srt=srt,
            burn_subtitles=burn_subtitles,
            project=project,
        )
    except AgentVoiceError as exc:
        typer.secho(f"{exc.error_code}: {exc.message}", fg=typer.colors.RED, err=True)
        if exc.suggestion:
            typer.echo(exc.suggestion, err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.secho(f"BAD_REQUEST: {_one_line(exc)}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    ctx = AppContext(settings)

    async def run():
        await ctx.workers.start()
        try:
            return await render_dub(ctx, payload)
        finally:
            await ctx.workers.stop()

    try:
        result = asyncio.run(run())
    except AgentVoiceError as exc:
        typer.secho(f"{exc.error_code}: {exc.message}", fg=typer.colors.RED, err=True)
        if exc.suggestion:
            typer.echo(exc.suggestion, err=True)
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt:
        typer.secho("interrupted", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=130) from None

    _print(result.model_dump(mode="json"))
    typer.secho(
        f"dub {result.dub_id}: {result.segments_count} segments "
        f"({result.cached_segments} cached) -> {result.file_path}",
        fg=typer.colors.GREEN,
    )
    for warning in result.overflow_warnings:
        typer.secho(
            f"warning: segment {warning.index} is {warning.audio_sec:.2f} s in a "
            f"{warning.slot_sec:.2f} s slot at {warning.atempo:.2f}x; shorten the line",
            fg=typer.colors.YELLOW,
        )


# ---------------------------------------------------------------------------
# doctor / mcp-config
# ---------------------------------------------------------------------------

# A check is PASS (usable), WARN (works, but degraded or unconfigured) or FAIL
# (this checkout cannot serve requests as configured). Only FAIL exits non-zero:
# no `.env` and a CPU-only box are both supported setups.
_CHECK_COLOUR = {
    "PASS": typer.colors.GREEN,
    "WARN": typer.colors.YELLOW,
    "FAIL": typer.colors.RED,
}

# Clients that get an MCP snippet. `Literal` makes Typer reject anything else
# with a usage error that lists the choices.
McpClient = Literal["claude-code", "claude-desktop", "vscode", "cursor", "command-code"]


@dataclass
class _Check:
    """One doctor row. ``fix`` is the single line to act on for a WARN/FAIL."""

    name: str
    status: Literal["PASS", "WARN", "FAIL"]
    detail: str = ""
    fix: str = ""


def _one_line(text: object) -> str:
    """Collapse a tool's multi-line message, so one check stays one row."""
    return " ".join(str(text or "").split())


def _print_checks(checks: list[_Check]) -> None:
    """Print `check | result | detail`, plus a `fix:` line under every warning.

    Plain `typer.echo` on purpose: the doctor runs before anything is installed,
    so it must not depend on a rich renderer being available.
    """
    header = ("check", "result", "detail")
    result_width = len(header[1])
    name_width = max([len(header[0])] + [len(check.name) for check in checks])
    detail_width = max(
        24, min(96, max([len(header[2])] + [len(check.detail) for check in checks]))
    )

    typer.echo(f"{header[0]:<{name_width}}  {header[1]:<{result_width}}  {header[2]}")
    typer.echo(f"{'-' * name_width}  {'-' * result_width}  {'-' * detail_width}")
    blank = f"{'':<{name_width}}  {'':<{result_width}}  "
    for check in checks:
        detail = check.detail
        if len(detail) > detail_width:
            detail = f"{detail[: detail_width - 3]}..."
        result = typer.style(
            f"{check.status:<{result_width}}", fg=_CHECK_COLOUR[check.status], bold=True
        )
        typer.echo(f"{check.name:<{name_width}}  {result}  {detail}")
        if check.fix and check.status != "PASS":
            typer.echo(f"{blank}fix: {check.fix}")


def _python_check() -> _Check:
    """The interpreter doctor runs under. pyproject pins 3.12, not just >=3.12."""
    import platform

    version = platform.python_version()
    if sys.version_info[:2] != (3, 12):
        return _Check(
            "python",
            "WARN",
            f"{version} (pyproject requires >=3.12,<3.13)",
            "run it as `uv run agent-voice doctor` so the project venv is used",
        )
    return _Check("python", "PASS", version)


def _probe_gpu() -> _Check:
    """nvidia-smi presence and answer. A CPU/mock-only box is never a FAIL."""
    import shutil
    import subprocess

    binary = shutil.which("nvidia-smi")
    if binary is None:
        return _Check(
            "gpu",
            "WARN",
            "no nvidia-smi: mock/CPU only",
            "install the NVIDIA driver for GPU engines; `--engine mock` needs no GPU",
        )

    # argv list, shell=False: no string ever reaches a shell.
    argv = [binary, "--query-gpu=name,memory.total", "--format=csv,noheader"]
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=20, shell=False, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _Check(
            "gpu",
            "WARN",
            f"nvidia-smi did not run: {_one_line(exc)}",
            "reinstall the NVIDIA driver, or use `--engine mock`",
        )

    stdout = _one_line(getattr(result, "stdout", ""))
    if getattr(result, "returncode", 1) != 0 or not stdout:
        reason = _one_line(getattr(result, "stderr", "")) or (
            f"exit code {getattr(result, 'returncode', '?')}"
        )
        return _Check(
            "gpu",
            "WARN",
            f"nvidia-smi failed: {reason}",
            "reinstall the NVIDIA driver, or use `--engine mock`",
        )
    return _Check("gpu", "PASS", stdout)


def _check_port(host: str, port: int, base_url: str) -> _Check:
    """Is `host:port` free, or already occupied by a healthy daemon?

    A bind is the only honest availability test. When it fails the port is taken,
    which is fine if the thing holding it answers `/health` (a running daemon);
    anything else would make `serve` fail with EADDRINUSE.

    The address is resolved with `getaddrinfo` and the socket is built from the
    first result, so the probe speaks the same family uvicorn will: an IPv6
    loopback bind (`AGENT_VOICE_HOST=::1`) is a supported setup, and an
    `AF_INET`-only socket could never bind it.
    """
    import socket

    import httpx

    label = f"{host}:{port}"
    try:
        family, socktype, proto, _canon, address = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )[0]
    except OSError as exc:
        return _Check(
            "port",
            "FAIL",
            f"{label} cannot be resolved ({_one_line(exc)})",
            "set AGENT_VOICE_HOST to an address this machine owns "
            "(127.0.0.1 or ::1 for loopback)",
        )
    try:
        with socket.socket(family, socktype, proto) as sock:
            # SO_REUSEADDR forgives TIME_WAIT, never a live listener.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(address)
    except OSError as exc:
        bind_error = _one_line(exc)
    else:
        return _Check("port", "PASS", f"{label} is free")

    # `base_url` comes from the settings as-is: it is bracketed for an IPv6 host.
    try:
        response = httpx.get(f"{base_url}/health", timeout=2.0)
        healthy = response.status_code == 200 and bool(response.json())
    except (httpx.HTTPError, ValueError):
        healthy = False
    if healthy:
        return _Check("port", "PASS", f"{label} already serves a healthy daemon")
    return _Check(
        "port",
        "FAIL",
        f"{label} in use ({bind_error})",
        f"stop the process holding {label}, or set AGENT_VOICE_PORT to a free port in .env",
    )


def _engine_checks(entries, engines_dir: Path, default_engine: str) -> list[_Check]:
    """One row per discovered engine; nothing installed is a FAIL, not a warning."""
    if not any(entry.installed for entry in entries):
        discovered = [entry.engine_id for entry in entries]
        # Suggest something that exists: the configured default when it was
        # discovered, else the first engine there is, else the bundled mock.
        if default_engine in discovered:
            suggested = default_engine
        else:
            suggested = discovered[0] if discovered else "mock"
        return [
            _Check(
                "engines",
                "FAIL",
                f"no engine installed in {engines_dir}; "
                f"discovered: {', '.join(discovered) or '(none)'}",
                f"agent-voice engines install {suggested}",
            )
        ]

    checks: list[_Check] = []
    for entry in entries:
        if entry.installed:
            checks.append(
                _Check(
                    f"engines: {entry.engine_id}",
                    "PASS",
                    f"{entry.backend_name} {entry.backend_version}",
                )
            )
            continue
        checks.append(
            _Check(
                f"engines: {entry.engine_id}",
                "WARN",
                entry.why_unavailable or "not installed",
                f"agent-voice engines install {entry.engine_id}",
            )
        )
    return checks


def _model_checks(entries) -> list[_Check]:
    """Which declared weights are actually on disk.

    `local/` repos ship with the engine and count as present, exactly like the
    router (`api/routing.py`) treats them — otherwise the dependency-free mock
    engine would report every model as missing.
    """
    present = 0
    missing: list[tuple[str, str, str]] = []
    for entry in entries:
        for model in entry.models:
            if model.downloaded_locally or model.hf_repo.startswith("local/"):
                present += 1
            else:
                missing.append((entry.engine_id, model.alias, model.hf_repo))

    total = present + len(missing)
    if total == 0:
        return [
            _Check(
                "models",
                "WARN",
                "no engine declares a model",
                "check that each engines/<id>/engine.yaml lists its models",
            )
        ]
    if not missing:
        return [_Check("models", "PASS", f"{present}/{total} weights present")]
    return [
        _Check(
            f"models: {engine_id}/{alias}",
            "WARN",
            f"{hf_repo} not downloaded",
            f"agent-voice models download {engine_id} {alias}",
        )
        for engine_id, alias, hf_repo in missing
    ]


def _inbox_check(settings) -> _Check:
    """Create the data dirs the daemon needs, and report the confinement root."""
    try:
        settings.ensure_dirs()
    except OSError as exc:
        return _Check(
            "inbox",
            "FAIL",
            f"not usable: {settings.inbox_dir} ({_one_line(exc)})",
            f"create {settings.data_dir}, or point DATA_DIR at a writable path",
        )
    return _Check("inbox", "PASS", str(settings.inbox_dir))


_STATUS_RANK = {"PASS": 0, "WARN": 1, "FAIL": 2}


def _default_engine_check(
    *, default_engine: str, known: list[str], installed: list[str]
) -> _Check:
    """Can the effective `AGENT_VOICE_DEFAULT_ENGINE` actually serve a request?"""
    fallback = installed[0] if installed else "mock"
    if not default_engine:
        return _Check(
            ".env",
            "FAIL",
            "AGENT_VOICE_DEFAULT_ENGINE is empty",
            f"set AGENT_VOICE_DEFAULT_ENGINE={fallback} in .env",
        )
    if not known:
        return _Check(
            ".env",
            "FAIL",
            f"AGENT_VOICE_DEFAULT_ENGINE={default_engine} resolves to no engine",
            "agent-voice engines install mock (the bundled, dependency-free engine)",
        )
    if default_engine not in known:
        return _Check(
            ".env",
            "FAIL",
            f"AGENT_VOICE_DEFAULT_ENGINE={default_engine} is not a discovered engine",
            f"set AGENT_VOICE_DEFAULT_ENGINE to one of {', '.join(known)} in .env",
        )
    if default_engine not in installed:
        return _Check(
            ".env",
            "WARN",
            f"AGENT_VOICE_DEFAULT_ENGINE={default_engine} is not installed",
            f"agent-voice engines install {default_engine}, or set "
            f"AGENT_VOICE_DEFAULT_ENGINE={fallback} in .env",
        )
    return _Check(".env", "PASS", f"AGENT_VOICE_DEFAULT_ENGINE={default_engine} (installed)")


def _env_check(
    *,
    default_engine: str,
    known: list[str],
    installed: list[str],
    engines_usable: bool = True,
) -> _Check:
    """`.env` presence, plus whether the configured default engine can serve.

    The two are judged independently: a checkout with no `.env` is a supported
    setup (WARN, `cp .env.example .env`), while the *effective* engine — whichever
    of the shell variable and the file wins — is always validated against the
    engines that really exist. Reporting presence alone used to hide a ghost
    `AGENT_VOICE_DEFAULT_ENGINE` in the environment behind a green run.

    Only engine ids are ever printed: a `.env` also holds API keys, and this
    command's output is the kind of thing a user pastes into an issue.
    """
    present = (Path.cwd() / ".env").is_file()
    presence = "present" if present else "absent, built-in defaults apply"
    fixes = [] if present else ["cp .env.example .env"]

    if not engines_usable:
        # One root cause, one row: the engines row already carries the FAIL.
        return _Check(
            ".env",
            "WARN",
            f"{presence}; default engine not checked (engines are unusable)",
            # The presence fix still applies; the engines row owns the other one.
            fixes[0] if fixes else "fix the engines row first, then re-run doctor",
        )

    engine = _default_engine_check(
        default_engine=default_engine, known=known, installed=installed
    )
    if engine.status != "PASS" and engine.fix:
        fixes.append(engine.fix)
    status = max(
        ("PASS" if present else "WARN", engine.status), key=_STATUS_RANK.__getitem__
    )
    return _Check(".env", status, f"{presence}; {engine.detail}", "; ".join(fixes))


def _validation_detail(exc: Exception) -> str:
    """One line naming the first offending key, e.g. `AGENT_VOICE_PORT: ...`.

    A pydantic `ValidationError` renders as five lines with a docs URL; a doctor
    row is one line, and the key is the only part the operator has to see.
    """
    errors = exc.errors() if hasattr(exc, "errors") else []
    if not errors:
        return _one_line(exc)
    first = errors[0]
    key = ".".join(str(part) for part in first.get("loc", ())) or "settings"
    detail = f"{key}: {first.get('msg', 'invalid value')}"
    if len(errors) > 1:
        detail += f" (+{len(errors) - 1} more)"
    return _one_line(detail)


def _fallback_settings() -> Settings:
    """Built-in defaults with `.env` ignored: what a broken config falls back to.

    `_env_file=None` drops the file, but a bad value exported in the *environment*
    would raise again; `model_construct()` then returns the plain field defaults,
    so the remaining doctor rows still print instead of a second traceback.
    """
    try:
        return Settings(_env_file=None)
    except (ValidationError, SettingsError):
        return Settings.model_construct()


def _espeak_install_hint() -> str:
    """The one command that installs espeak-ng on this platform."""
    if sys.platform == "darwin":
        return "brew install espeak-ng"
    if sys.platform.startswith("win"):
        return (
            "install espeak-ng (https://github.com/espeak-ng/espeak-ng/releases) "
            "and put espeak-ng.exe on PATH"
        )
    return "sudo apt install espeak-ng (or your distribution's package)"


def _espeak_check(entries) -> _Check | None:
    """espeak-ng presence, reported only when the shipped Kokoro engine exists.

    Kokoro's English G2P is spaCy-based; espeak-ng is its out-of-dictionary
    fallback and the backend for every non-English voice. Common English still
    speaks without it, so a missing binary is a WARN carrying the install
    command — never a FAIL — and a checkout that does not ship `engines/kokoro/`
    gets no row at all.
    """
    if "kokoro" not in {entry.engine_id for entry in entries}:
        return None

    from .kokoro_support import espeak_ng_status

    path, version = espeak_ng_status()
    if path is None:
        return _Check(
            "espeak-ng",
            "WARN",
            "not on PATH (Kokoro out-of-dictionary and non-English G2P)",
            _espeak_install_hint(),
        )
    return _Check("espeak-ng", "PASS", f"{path} {version}" if version else path)


def _asr_check(entries, settings) -> _Check:
    """Is the configured default ASR engine actually installed?

    A WARN, never a FAIL: speaking works fine with no ASR engine at all, and a
    checkout that never transcribes should not fail `doctor`. When the engine *is*
    there, name its aliases — that is the value a caller would otherwise get by
    running `models list`.
    """
    engine_id = settings.default_asr_engine
    installed = {entry.engine_id for entry in entries if entry.installed}
    discovered = {entry.engine_id for entry in entries}

    if engine_id not in installed:
        if engine_id not in discovered:
            detail = f"AGENT_VOICE_DEFAULT_ASR_ENGINE={engine_id} is not a discovered engine"
        else:
            detail = f"AGENT_VOICE_DEFAULT_ASR_ENGINE={engine_id} is not installed"
        return _Check(
            "asr",
            "WARN",
            detail,
            f"agent-voice engines install {engine_id} (needed for transcribe/dubbing)",
        )

    aliases = [
        model.alias
        for entry in entries
        if entry.engine_id == engine_id
        for model in entry.models
    ]
    return _Check(
        "asr",
        "PASS",
        f"{engine_id}: {', '.join(aliases) or '(no models declared)'}",
    )


def _dub_filters_check() -> _Check:
    """Does this ffmpeg have the filters a ducked dubbing mix needs?

    `sidechaincompress` + `alimiter` are what make `keep_original_bed=True` duck
    the source under the voice instead of burying it. An older or minimal ffmpeg
    build can lack them; dubbing still completes with a plain mix, so this is a
    WARN. `ffmpeg` itself missing is already a FAIL on the ffmpeg row — this check
    never duplicates it.
    """
    import shutil
    import subprocess

    binary = shutil.which("ffmpeg")
    if binary is None:
        # The ffmpeg row owns the failure. One root cause, one row.
        return _Check("dub filters", "PASS", "skipped: ffmpeg row reports the problem")

    # argv list, shell=False: no string ever reaches a shell.
    argv = [binary, "-hide_banner", "-filters"]
    try:
        result = subprocess.run(argv, shell=False, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return _Check(
            "dub filters",
            "WARN",
            f"ffmpeg -filters did not run: {_one_line(exc)}",
            "dubbing with keep_original_bed=true will fall back to a plain mix",
        )

    listing = (result.stdout or b"").decode("utf-8", "replace")
    missing = [name for name in ("sidechaincompress", "alimiter") if name not in listing]
    if not missing:
        return _Check("dub filters", "PASS", "sidechaincompress, alimiter")
    return _Check(
        "dub filters",
        "WARN",
        f"missing: {', '.join(missing)}",
        "dubbing with keep_original_bed=true will fall back to a plain mix",
    )


@app.command()
def doctor() -> None:
    """Check this machine and checkout: tools, GPU, port, engines, weights, config.

    One row per check and one `fix:` line under every WARN/FAIL. Exits non-zero
    only on a FAIL, so a CPU-only box or a missing `.env` (both supported) still
    return 0. Engine and model code stays imported inside this body: the doctor
    must run on a machine that cannot import torch at all.
    """
    import shutil

    from .engine_registry import EngineRegistry

    # A bad `.env` (a non-numeric port, an unknown enum value) must be a FAIL row,
    # not a traceback: like the engines row, this is the command you run *because*
    # the configuration is wrong. The rest of the run uses built-in defaults so
    # every other row still reports.
    settings_error: _Check | None = None
    try:
        settings = get_settings()
    except (ValidationError, SettingsError) as exc:
        settings_error = _Check(
            "config",
            "FAIL",
            _validation_detail(exc),
            "fix .env (or the AGENT_VOICE_* variable overriding it) and re-run doctor; "
            "the rows below use built-in defaults",
        )
        settings = _fallback_settings()
    default_engine = settings.default_engine

    # A broken ENGINES_DIR must show up as a FAIL row, not a traceback: the
    # doctor is the command you run *because* the layout is wrong.
    registry_error: _Check | None = None
    known: list[str] = []
    entries: list = []
    try:
        registry = EngineRegistry(settings.engines_dir)
        entries = registry.list_entries()
        known = registry.ids()
    except OSError as exc:
        registry_error = _Check(
            "engines",
            "FAIL",
            # Reason first: a long path would otherwise eat the whole detail.
            f"not usable: {settings.engines_dir} ({_one_line(exc)})",
            f"create {settings.engines_dir}, or point ENGINES_DIR at the checkout",
        )
    installed = [entry.engine_id for entry in entries if entry.installed]

    checks: list[_Check] = [_python_check()]

    uv = shutil.which("uv")
    checks.append(
        _Check("uv", "PASS", uv)
        if uv
        else _Check(
            "uv",
            "FAIL",
            "not on PATH",
            "install uv: https://docs.astral.sh/uv/getting-started/installation/",
        )
    )

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    missing_tools = [
        name for name, found in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not found
    ]
    if missing_tools:
        checks.append(
            _Check(
                "ffmpeg",
                "FAIL",
                f"missing on PATH: {', '.join(missing_tools)}",
                "install ffmpeg with ffprobe: `sudo apt install ffmpeg` "
                "(macOS: `brew install ffmpeg`)",
            )
        )
    else:
        checks.append(_Check("ffmpeg", "PASS", f"{ffmpeg} (ffprobe present)"))
    # Directly after the ffmpeg row: it is a property of that same binary. Never a
    # FAIL — without these two filters a ducked dub mix degrades to a plain one.
    checks.append(_dub_filters_check())

    checks.append(_probe_gpu())
    # Engine-specific tool rows (`espeak-ng` for Kokoro) belong with the tool
    # probes above, before `.env` decides which engine is expected to be
    # installed. A missing optional tool is a WARN, never a FAIL.
    espeak = _espeak_check(entries)
    if espeak is not None:
        checks.append(espeak)
    if settings_error is not None:
        checks.append(settings_error)
    # `.env` before the engine rows: it decides which engine is *expected* to be
    # installed, and a bad value there explains a confusing engine row.
    checks.append(
        _env_check(
            default_engine=default_engine,
            known=known,
            installed=installed,
            engines_usable=registry_error is None,
        )
    )
    if registry_error is not None:
        checks.append(registry_error)
    else:
        checks.extend(_engine_checks(entries, settings.engines_dir, default_engine))
        # After the engine rows and before the weights: an ASR engine is a separate
        # plugin family from the speaking default, so its own row belongs next to
        # the engines it is judged against. WARN only — a TTS-only box is fine.
        checks.append(_asr_check(entries, settings))
        checks.extend(_model_checks(entries))
    checks.append(_inbox_check(settings))
    # Daemon health last: it is the only row that can legitimately be "in use".
    checks.append(_check_port(settings.host, settings.port, settings.base_url))

    _print_checks(checks)
    failures = [check for check in checks if check.status == "FAIL"]
    warnings = [check for check in checks if check.status == "WARN"]
    if failures:
        typer.secho(
            f"doctor: {len(failures)} failed, {len(warnings)} warning(s) — "
            "fix the FAIL rows above",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    typer.secho(
        f"doctor: {len(checks)} checks, {len(warnings)} warning(s), no failures",
        fg=typer.colors.GREEN,
    )


@app.command("mcp-config")
def mcp_config(
    client: McpClient = typer.Option(
        ...,
        "--client",
        help="Target client: claude-code, claude-desktop, vscode, cursor or command-code.",
    ),
) -> None:
    """Print a ready-to-paste MCP server snippet for one client.

    JSON on stdout with the absolute path to this checkout, so it needs neither a
    daemon nor a network: paste it into the client's config file under the key
    that client expects. `AGENT_VOICE_CLIENT_ID` becomes the `X-Agent-Id` header,
    which selects that client's per-agent binding.
    """
    root = Path(__file__).resolve().parents[2]
    server: dict[str, object] = {
        "command": "uv",
        "args": ["run", "--directory", str(root), "agent-voice", "mcp"],
        "env": {"AGENT_VOICE_CLIENT_ID": client},
    }

    if client == "vscode":
        # VS Code's `.vscode/mcp.json` uses a `servers` key and spells out the
        # transport; the other four clients share Claude's `mcpServers` shape.
        payload: dict[str, object] = {"servers": {"agent-voice": {"type": "stdio", **server}}}
    else:
        payload = {"mcpServers": {"agent-voice": server}}

    typer.echo(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# engines
# ---------------------------------------------------------------------------


@engines_app.command("list")
def engines_list() -> None:
    """List discovered engines with install state."""
    from .engine_registry import EngineRegistry

    settings = get_settings()
    registry = EngineRegistry(settings.engines_dir)
    _print([entry.model_dump(mode="json") for entry in registry.list_entries()])


@engines_app.command("install")
def engines_install(engine_id: str) -> None:
    """Create the engine's isolated venv with `uv sync`."""
    import shutil
    import subprocess

    from .engine_registry import EngineRegistry

    settings = get_settings()
    registry = EngineRegistry(settings.engines_dir)
    record = registry.get(engine_id)

    pyproject = record.directory / "pyproject.toml"
    if not pyproject.is_file():
        typer.secho(
            f"engine '{engine_id}' has no pyproject.toml; nothing to install",
            fg=typer.colors.YELLOW,
        )
        return

    uv = shutil.which("uv")
    if uv is None:
        typer.secho("uv is not on PATH", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.echo(f"installing {engine_id} ...")
    # argv list, shell=False: no string ever reaches a shell.
    result = subprocess.run(
        [uv, "sync", "--project", str(record.directory)],
        cwd=str(record.directory),
        shell=False,
    )
    if result.returncode != 0:
        typer.secho("install failed", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=result.returncode)

    registry.reload()
    refreshed = registry.get(engine_id)
    if refreshed.installed:
        typer.secho(f"{engine_id} installed", fg=typer.colors.GREEN)
    else:
        typer.secho(
            f"{engine_id} still uninstalled: {refreshed.why_unavailable}",
            fg=typer.colors.YELLOW,
        )


# -- staged plugins (REQUIRE_ENGINE_APPROVAL) --------------------------------


@engines_app.command("pending")
def engines_pending() -> None:
    """List plugins staged under engines/.pending/ awaiting approval."""
    from .engine_registry import EngineRegistry

    settings = get_settings()
    registry = EngineRegistry(settings.engines_dir)
    _print(
        [
            {"engine_id": engine_id, "path": str(registry.pending_dir / engine_id)}
            for engine_id in registry.list_pending()
        ]
    )


@engines_app.command("approve")
def engines_approve(
    engine_id: str,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt (for scripts)."
    ),
) -> None:
    """Promote a staged plugin to engines/<id>/, after showing what it contains."""
    import hashlib

    from .engine_registry import EngineRegistry
    from .schemas import AgentVoiceError

    settings = get_settings()
    registry = EngineRegistry(settings.engines_dir)
    staged = registry.pending_dir / engine_id

    if not staged.is_dir():
        typer.secho(f"no staged plugin at {staged}", fg=typer.colors.RED, err=True)
        typer.echo(f"staged: {', '.join(registry.list_pending()) or '(none)'}")
        raise typer.Exit(code=1)

    # Show the operator what they are about to activate. This is the whole point
    # of staging: an agent wrote this code, and a human approves it after reading
    # what it claims to be and how it differs from what was reviewed.
    manifest = None
    try:
        import yaml

        from .engine_registry import EngineManifest

        manifest = EngineManifest(
            **yaml.safe_load((staged / "engine.yaml").read_text("utf-8"))
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable manifest blocks approval
        typer.secho(f"staged manifest is not usable: {exc}", fg=typer.colors.RED, err=True)
        typer.echo("Nothing was moved.", err=True)
        raise typer.Exit(code=1) from exc

    worker_path = staged / "worker.py"
    worker_sha = (
        hashlib.sha256(worker_path.read_bytes()).hexdigest()
        if worker_path.is_file()
        else "(no worker.py)"
    )
    typer.echo(f"plugin    : {manifest.engine_id}")
    typer.echo(f"backend   : {manifest.backend_name} {manifest.backend_version}")
    typer.echo(f"models    : {[m.alias for m in manifest.models] or '(none)'}")
    typer.echo(f"worker.py : {worker_sha}")
    typer.echo(f"from      : {staged}")

    if not yes:
        typer.confirm("Activate this plugin? It will be runnable by the daemon.", abort=True)

    try:
        record = registry.approve_engine(engine_id)
    except AgentVoiceError as exc:
        typer.secho(f"{exc.error_code}: {exc.message}", fg=typer.colors.RED, err=True)
        if exc.suggestion:
            typer.echo(exc.suggestion, err=True)
        typer.echo("Nothing was moved.", err=True)
        raise typer.Exit(code=1) from exc

    _print(
        {
            "approved": engine_id,
            "to": str(record.directory),
            "worker_sha256": worker_sha,
            "installed": record.installed,
            "why_unavailable": record.why_unavailable,
        }
    )
    if not record.installed:
        typer.echo(
            f"next: `agent-voice engines install {engine_id}` — {record.why_unavailable}"
        )
    else:
        typer.secho(f"{engine_id} approved and active", fg=typer.colors.GREEN)


# -- IPC v1 conformance ------------------------------------------------------

STEP_NAMES: tuple[str, ...] = (
    "spawn+connect",
    "hello",
    "load_model",
    "synthesize",
    "cancel",
    "health",
    "shutdown",
)
"""The TTS step table: what a `kind: tts` worker must pass."""

ASR_STEP_NAMES: tuple[str, ...] = (
    "spawn+connect",
    "hello",
    "load_model",
    "transcribe",
    "cancel",
    "health",
    "shutdown",
)
"""The ASR step table. Same length and the same tail as :data:`STEP_NAMES`.

Deliberately a sibling tuple rather than a dict keyed by kind: the table shape is
what makes two runs comparable, and one row differs between the two families —
`synthesize` becomes `transcribe`. Kept in step with :data:`STEP_NAMES` by length
so a future step added to one is visibly missing from the other.
"""


@dataclass
class _Step:
    """One row of the conformance table. ``ok is None`` means skipped."""

    name: str
    ok: bool | None
    detail: str = ""


_RESULT_TEXT = {True: "PASS", False: "FAIL", None: "SKIP"}
_RESULT_COLOUR = {
    True: typer.colors.GREEN,
    False: typer.colors.RED,
    None: typer.colors.YELLOW,
}


def _print_steps(steps: list[_Step]) -> None:
    """Print `step | result | detail` as one aligned, scannable table."""
    header = ("step", "result", "detail")
    result_width = len(header[1])
    name_width = max([len(header[0])] + [len(step.name) for step in steps])
    detail_width = max(20, min(100, max([len(header[2])] + [len(s.detail) for s in steps])))

    typer.echo(f"{header[0]:<{name_width}}  {header[1]:<{result_width}}  {header[2]}")
    typer.echo(f"{'-' * name_width}  {'-' * result_width}  {'-' * detail_width}")
    for step in steps:
        detail = step.detail
        if len(detail) > detail_width:
            detail = f"{detail[: detail_width - 3]}..."
        result = _RESULT_TEXT[step.ok]
        coloured = typer.style(
            f"{result:<{result_width}}", fg=_RESULT_COLOUR[step.ok], bold=True
        )
        typer.echo(f"{step.name:<{name_width}}  {coloured}  {detail}")


@engines_app.command("test")
def engines_test(engine_id: str) -> None:
    """Run the full IPC v1 conformance sequence against a real worker.

    Exits non-zero if any step fails. `agent-voice engines test mock` needs no GPU.
    """
    import asyncio

    from .engine_registry import EngineRegistry
    from .schemas import AgentVoiceError

    settings = get_settings()
    settings.ensure_dirs()
    registry = EngineRegistry(settings.engines_dir)
    try:
        record = registry.require_installed(engine_id)
    except AgentVoiceError as exc:
        typer.secho(f"{exc.error_code}: {exc.message}", fg=typer.colors.RED, err=True)
        if exc.suggestion:
            typer.echo(exc.suggestion, err=True)
        raise typer.Exit(code=1) from exc

    try:
        steps = asyncio.run(
            _run_conformance(record=record, registry=registry, settings=settings)
        )
    except KeyboardInterrupt:
        typer.secho("interrupted", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=130) from None

    _print_steps(steps)
    if not all(step.ok for step in steps):
        typer.secho("conformance failed", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho(f"{engine_id}: IPC v1 conformance passed", fg=typer.colors.GREEN)


def _asr_probe_wav(path: Path, seconds: float) -> Path:
    """Write a deterministic 16 kHz mono WAV for the ASR conformance steps.

    Two sine partials rather than clean silence: a real ASR backend given pure
    silence legitimately returns no segments at all, and a conformance step that
    demands words from silence would fail a healthy worker. A tone is not speech,
    so the step still only requires a well-formed reply, never a transcript.
    """
    import numpy as np
    import soundfile as sf

    sr = 16_000
    n = max(1, int(sr * seconds))
    t = np.linspace(0.0, seconds, n, endpoint=False)
    signal = 0.18 * np.sin(2 * np.pi * 180 * t) + 0.10 * np.sin(2 * np.pi * 640 * t)
    sf.write(path, signal.astype(np.float32), sr, subtype="PCM_16")
    return path


def _reference_voice(voices) -> tuple[str, str, bool]:
    """Pick a reference voice: the configured default, else the first registered.

    A cloning engine cannot synthesise at all without one, so this runs before the
    synthesize step and reports honestly when there is nothing to use.
    """
    from .schemas import AgentVoiceError

    settings = get_settings()
    candidate = None
    if settings.default_voice_id:
        try:
            candidate = voices.resolve(settings.default_voice_id)
        except AgentVoiceError:
            candidate = None
    if candidate is None:
        registered = voices.list()
        candidate = registered[0] if registered else None
    if candidate is None:
        return "", "", False
    return (
        str(voices.ref_wav_path(candidate.voice_id)),
        candidate.ref_text,
        candidate.x_vector_only_mode,
    )


def _probe_voice(voices, tmpdir: Path):
    """Register a synthetic reference voice so a cloning engine can be tested.

    The Qwen Base checkpoints cannot synthesise at all without a reference clip,
    so `engines test <cloning engine>` would fail on a clean checkout with no
    voices registered. A deterministic 6 s formant tone is enough to exercise the
    whole pipeline; it is registered under a reserved name and removed by the
    caller in a finally block.
    """
    import numpy as np
    import soundfile as sf

    sr = 24_000
    seconds = 6.0
    n = int(sr * seconds)
    t = np.linspace(0, seconds, n, endpoint=False)
    signal = (
        0.18 * np.sin(2 * np.pi * 180 * t)
        + 0.10 * np.sin(2 * np.pi * 640 * t)
        + 0.05 * np.sin(2 * np.pi * 1200 * t)
    )
    path = tmpdir / "probe-ref.wav"
    sf.write(path, signal.astype(np.float32), sr, subtype="PCM_16")
    return voices.add(
        name="agent-voice-probe",
        source_path=path,
        ref_text="This is a synthetic reference clip used for engine conformance testing.",
        x_vector_only=False,
    )


def _skip_rest(
    steps: list[_Step], reason: str, *, names: tuple[str, ...] = STEP_NAMES
) -> list[_Step]:
    """Mark every step not yet appended as SKIP, so the table keeps its shape.

    ``names`` is the table the run is filling in, passed in rather than read from
    the module: a TTS run fills :data:`STEP_NAMES`, an ASR run :data:`ASR_STEP_NAMES`,
    and reading one of them unconditionally would print the wrong row labels for
    the other.
    """
    for name in names[len(steps) :]:
        steps.append(_Step(name, None, reason))
    return steps


def _conformance_target(registry, engine_id: str) -> tuple[str, str | None, str]:
    """Pick ``(kind, speaker, model_alias)`` for the conformance compute step.

    ``kind`` is a *step kind*, not a model mode: ``"transcribe"`` for an
    ``kind: asr`` engine, otherwise the model mode to synthesise with
    (``"clone"`` / ``"custom_voice"``). The ASR branch runs first and returns
    immediately, because an ASR manifest has no ``mode`` to select on at all.

    For TTS, a clone model comes first: cloning is the v1 path every engine had to
    support before v0.4.0, and CustomVoice/VoiceDesign aliases can share its quality
    tier, so an unfiltered pick could load a checkpoint that cannot clone.

    A preset-only engine has no clone alias at all — Kokoro declares
    ``voice_clone: false`` and a single ``custom_voice`` model — so it is
    exercised through that model's first ``preset_speakers`` entry, with both
    ``mode`` and ``speaker`` sent exactly as routing would for
    ``speak(speaker=...)``. Without this fallback a healthy CPU engine could
    never pass its own conformance gate.
    """
    from .schemas import AgentVoiceError

    if registry.get(engine_id).manifest.kind == "asr":
        # No reference clip and no preset timbre on this path: a transcription
        # needs an audio file and a model alias, both of which the step builds.
        return "transcribe", None, registry.default_asr_model_alias(engine_id)

    try:
        alias = registry.default_model_alias(engine_id, mode="clone")
        return "clone", None, alias
    except AgentVoiceError as clone_error:
        try:
            alias = registry.default_model_alias(engine_id, mode="custom_voice")
        except AgentVoiceError:
            # Report the clone complaint: "no clone model" is the honest summary,
            # and MODEL_NOT_FOUND for an empty engine is identical either way.
            raise clone_error from None
        spec = registry.model_spec(engine_id, alias)
        speaker = spec.preset_speakers[0] if spec.preset_speakers else None
        return "custom_voice", speaker, alias


async def _conformance_tail(
    *, handle, steps: list[_Step], names: tuple[str, ...]
) -> list[_Step]:
    """The `health` → `shutdown` tail, shared by both engine families.

    Both kinds of worker multiplex the same JSON RPCs, so a health/shutdown check
    that ran only for TTS would leave half the daemon's workers unverified.
    """
    import asyncio
    import time
    import uuid

    # -- health -----------------------------------------------------
    # The protocol's health RPC is what a manager uses to check a live
    # worker, so a conformance run that never calls it is not exercising v1.
    try:
        reply = await handle.connection.request(
            {"id": uuid.uuid4().hex, "method": "health"}, timeout=15.0
        )
    except (ConnectionError, TimeoutError, OSError) as exc:
        steps.append(_Step("health", False, f"no reply: {exc}"))
        return _skip_rest(steps, "worker did not answer health", names=names)

    health_ok = reply.get("error_code") is None and reply.get("ok") is True
    if health_ok:
        detail = (
            f"ok=True loaded_models={reply.get('loaded_models')} "
            f"in_flight={reply.get('in_flight')}"
        )
    else:
        detail = f"{reply.get('error_code')}: {reply.get('message')}"
    steps.append(_Step("health", health_ok, detail))

    # -- shutdown ---------------------------------------------------
    started = time.perf_counter()
    try:
        reply = await handle.connection.request(
            {"id": uuid.uuid4().hex, "method": "shutdown"}, timeout=15.0
        )
    except (ConnectionError, TimeoutError, OSError) as exc:
        steps.append(_Step("shutdown", False, f"no acknowledgement: {exc}"))
        return steps

    acknowledged = reply.get("error_code") is None and reply.get("ok") is True
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and handle.process.poll() is None:
        await asyncio.sleep(0.05)
    exit_code = handle.process.poll()
    shipped = time.perf_counter() - started
    if not acknowledged:
        detail = f"{reply.get('error_code')}: {reply.get('message')}"
    elif exit_code is None:
        detail = "acknowledged, but the process is still alive after 15 s"
    else:
        detail = f"acknowledged, exited rc={exit_code} in {shipped:.2f} s"
    steps.append(_Step("shutdown", bool(acknowledged and exit_code is not None), detail))
    return steps


async def _conformance_transcribe(
    *, handle, workdir: Path, alias: str, steps: list[_Step]
) -> bool:
    """The ASR compute step. Returns False when the table should stop here.

    A 1 s tone is deliberate: a real backend handed silence may legitimately emit
    no segments, so demanding words would fail a healthy worker. What *is*
    required is a well-formed reply — no `error_code`, `segments` a list, `text` a
    string — because that is what the parent actually parses.
    """
    import time
    import uuid

    audio = _asr_probe_wav(workdir / "conformance-asr.wav", 1.0)
    started = time.perf_counter()
    try:
        reply = await handle.connection.request(
            {
                "id": uuid.uuid4().hex,
                "method": "transcribe",
                "model_alias": alias,
                "audio_path": str(audio),
                "language": None,
                "word_timestamps": True,
                "hotwords": [],
            },
            timeout=1800.0,
        )
    except (ConnectionError, TimeoutError, OSError) as exc:
        steps.append(_Step("transcribe", False, f"IPC error: {exc}"))
        return False

    elapsed = time.perf_counter() - started
    segments = reply.get("segments")
    text = reply.get("text")
    ok = (
        reply.get("error_code") is None
        and isinstance(segments, list)
        and isinstance(text, str)
    )
    if reply.get("error_code") is not None:
        detail = f"{reply.get('error_code')}: {reply.get('message')}"
    elif not isinstance(segments, list):
        detail = f"segments is {type(segments).__name__}, expected a list"
    elif not isinstance(text, str):
        detail = f"text is {type(text).__name__}, expected a string"
    else:
        detail = f"{len(segments)} segments, {len(text)} chars in {elapsed:.2f} s"
    steps.append(_Step("transcribe", ok, detail))
    return ok


async def _conformance_asr_cancel(
    *, handle, workdir: Path, alias: str, steps: list[_Step]
) -> None:
    """Exercise `cancel` against a transcribe that may still be decoding.

    A CPU decode of a short clip can finish faster than the round trip, and
    claiming a cancellation that did not happen would be a lie. So the step
    passes on a **well-formed** `{cancelled: bool}` reply and records which of the
    two things actually happened. The audio is long on purpose: it gives a real
    backend something to still be working on when the cancel arrives.

    The transcribe is issued from a background thread (`run_coroutine_threadsafe`
    onto this loop) so the socket reader stays free to carry the `cancel`, exactly
    as the manager issues its own requests.
    """
    import asyncio
    import threading
    import time
    import uuid

    long_audio = _asr_probe_wav(workdir / "conformance-asr-long.wav", 20.0)
    request_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    submitted = threading.Event()
    outcome: dict[str, object] = {}

    def worker() -> None:
        try:
            future = asyncio.run_coroutine_threadsafe(
                handle.connection.request(
                    {
                        "id": request_id,
                        "method": "transcribe",
                        "model_alias": alias,
                        "audio_path": str(long_audio),
                        "language": None,
                        "word_timestamps": False,
                        "hotwords": [],
                    },
                    timeout=1800.0,
                ),
                loop,
            )
            submitted.set()
            outcome["reply"] = future.result(timeout=1800.0)
        except Exception as exc:  # noqa: BLE001 - reported, never raised into the table
            submitted.set()
            outcome["error"] = exc

    handle.in_flight.add(request_id)
    started = time.perf_counter()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    await asyncio.to_thread(submitted.wait, 10.0)

    # Wait until the worker itself says a request is in flight, rather than
    # assuming it: `health` is the protocol's own answer to that question, and a
    # cancel sent before the decode starts would prove nothing.
    in_flight = False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and thread.is_alive():
        try:
            probe = await handle.connection.request(
                {"id": uuid.uuid4().hex, "method": "health"}, timeout=5.0
            )
        except (ConnectionError, TimeoutError, OSError):
            break
        if int(probe.get("in_flight") or 0) >= 1:
            in_flight = True
            break
        await asyncio.sleep(0.02)

    try:
        reply = await handle.connection.request(
            {
                "id": uuid.uuid4().hex,
                "method": "cancel",
                "target_request_id": request_id,
            },
            timeout=10.0,
        )
    except (ConnectionError, TimeoutError, OSError) as exc:
        reply = {"error_code": "IPC_ERROR", "message": str(exc)}

    cancelled = reply.get("cancelled")
    ok = reply.get("error_code") is None and isinstance(cancelled, bool)
    if reply.get("error_code") is not None:
        detail = (
            f"no cancel acknowledgement: {reply.get('error_code')} "
            f"{reply.get('message', '')}"
        ).strip()
    elif not isinstance(cancelled, bool):
        detail = f"cancelled is {type(cancelled).__name__}, expected a bool"
    elif cancelled:
        detail = f"worker acknowledged cancel after {time.perf_counter() - started:.2f} s"
    elif in_flight:
        detail = "transcribe finished before the cancel landed (cancelled=False)"
    else:
        detail = "transcribe finished before cancel landed (never in flight)"
    steps.append(_Step("cancel", ok, detail))

    # Let the background request settle before shutdown, so the last row is not
    # racing a live decode on a socket that is about to close.
    with contextlib.suppress(Exception):
        await asyncio.to_thread(thread.join, 180.0)
    handle.in_flight.discard(request_id)


async def _run_conformance(*, record, registry, settings) -> list[_Step]:
    """Drive one worker: spawn → hello → load → synthesize|transcribe → cancel.

    The table is chosen from the manifest's ``kind``: a ``tts`` engine fills
    :data:`STEP_NAMES`, an ``asr`` engine :data:`ASR_STEP_NAMES`. Everything after
    the compute step — cancel, health, shutdown — is shared, because it exercises
    the same JSON multiplexing on either kind of worker.
    """
    import asyncio
    import shutil
    import tempfile
    import time
    import uuid

    from .cache import SynthesisCache
    from .schemas import AgentVoiceError, ErrorCode
    from .voice_store import VoiceStore
    from .worker_manager import WorkerManager

    engine_id = record.engine_id
    # One table for the whole run: every _skip_rest call needs the labels of the
    # rows that will never be appended, and a mixed table would mislabel them.
    step_names = ASR_STEP_NAMES if record.manifest.kind == "asr" else STEP_NAMES
    steps: list[_Step] = []
    workdir = Path(tempfile.mkdtemp(prefix="agent-voice-conformance-"))
    voices = VoiceStore(settings.voices_dir, max_ref_text_chars=settings.max_ref_text_chars)
    manager = WorkerManager(
        settings=settings,
        registry=registry,
        cache=SynthesisCache(settings.cache_dir, max_gb=settings.max_cache_gb),
        voices=voices,
    )

    await manager.start()
    handle = None
    try:
        # -- spawn + connect + hello ------------------------------------
        # WorkerManager performs the handshake while spawning, so a bad hello
        # surfaces there. A MANIFEST_INVALID complaint about the protocol means
        # the round-trip *did* complete and only the version was wrong.
        started = time.perf_counter()
        try:
            handle = await manager.ensure_worker(engine_id)
        except AgentVoiceError as exc:
            reached_hello = (
                exc.error_code is ErrorCode.MANIFEST_INVALID and "protocol" in exc.message
            )
            steps.append(
                _Step(
                    "spawn+connect",
                    reached_hello,
                    "IPC connected and hello round-trip completed"
                    if reached_hello
                    else f"{exc.error_code}: {exc.message}",
                )
            )
            steps.append(_Step("hello", False, f"{exc.error_code}: {exc.message}"))
            return _skip_rest(steps, "not attempted", names=step_names)
        elapsed = time.perf_counter() - started
        steps.append(
            _Step("spawn+connect", True, f"pid {handle.process.pid}, ready in {elapsed:.2f} s")
        )

        hello = handle.hello
        hello_ok = hello.get("protocol_version") == 1 and hello.get("engine_id") == engine_id
        steps.append(
            _Step(
                "hello",
                hello_ok,
                f"protocol_version={hello.get('protocol_version')} "
                f"engine_id={hello.get('engine_id')} "
                f"backend={hello.get('backend_name')} {hello.get('backend_version')} "
                f"streaming={bool(hello.get('capabilities', {}).get('streaming'))}",
            )
        )
        if not hello_ok:
            return _skip_rest(steps, "hello handshake invalid", names=step_names)

        # -- load_model -------------------------------------------------
        # Clone first, preset timbres second: the target decides which step runs
        # and what it sends, so it is resolved before any load.
        try:
            kind, speaker, alias = _conformance_target(registry, engine_id)
        except AgentVoiceError as exc:
            steps.append(_Step("load_model", False, exc.message))
            return _skip_rest(steps, "no model alias to load", names=step_names)
        try:
            reply = await handle.connection.request(
                {"id": uuid.uuid4().hex, "method": "load_model", "model_alias": alias},
                timeout=900.0,
            )
        except (ConnectionError, TimeoutError, OSError) as exc:
            steps.append(_Step("load_model", False, f"IPC error: {exc}"))
            return _skip_rest(steps, "worker connection lost", names=step_names)

        load_ok = reply.get("error_code") is None and reply.get("model_alias") == alias
        detail = (
            f"alias={alias} as={kind} loaded_models={reply.get('loaded_models')}"
            if reply.get("error_code") is None
            else f"{reply.get('error_code')}: {reply.get('message')}"
        )
        steps.append(_Step("load_model", load_ok, detail))
        if not load_ok:
            return _skip_rest(steps, "model failed to load", names=step_names)

        # -- transcribe (asr) or synthesize (tts) ------------------------
        # One row differs between the two families; the cancel/health/shutdown
        # tail below is shared because it exercises the same JSON multiplexing.
        if kind == "transcribe":
            finished = await _conformance_transcribe(
                handle=handle, workdir=workdir, alias=alias, steps=steps
            )
            if not finished:
                return _skip_rest(
                    steps, "cannot exercise cancel after a failed transcribe",
                    names=step_names,
                )
            await _conformance_asr_cancel(
                handle=handle, workdir=workdir, alias=alias, steps=steps
            )
            return await _conformance_tail(handle=handle, steps=steps, names=step_names)

        # -- synthesize -------------------------------------------------
        # A clone target speaks a reference clip; a preset target must not send
        # one (routing refuses voice + custom_voice), so it speaks its speaker.
        # `kind` is the model mode here: only "transcribe" left the TTS path.
        ref_wav, ref_text, x_vector_only = "", "", False
        if kind == "clone":
            ref_wav, ref_text, x_vector_only = _reference_voice(voices)
            if not ref_wav and record.manifest.capabilities.voice_clone:
                # A cloning engine cannot synthesise without a reference, so a
                # clean checkout would fail here. Register a synthetic one for the
                # duration of the test instead of demanding manual setup.
                probe = _probe_voice(voices, workdir)
                ref_wav = str(voices.ref_wav_path(probe.voice_id))
                ref_text = probe.ref_text
                x_vector_only = probe.x_vector_only_mode
                typer.echo(
                    "  (no voice registered: using a synthetic probe voice for conformance)"
                )

        wav_path = workdir / "conformance.wav"
        started = time.perf_counter()
        try:
            reply = await handle.connection.request(
                {
                    "id": uuid.uuid4().hex,
                    "method": "synthesize",
                    "model_alias": alias,
                    "text": "Conformance check, one two three.",
                    "ref_wav_path": ref_wav,
                    "ref_text": ref_text,
                    "x_vector_only_mode": x_vector_only,
                    "mode": kind,
                    "speaker": speaker,
                    "prompt_cache_path": None,
                    "wav_path": str(wav_path),
                    "language": "Auto",
                    "seed": None,
                    "speed": 1.0,
                },
                timeout=1800.0,
            )
        except (ConnectionError, TimeoutError, OSError) as exc:
            steps.append(_Step("synthesize", False, f"IPC error: {exc}"))
            return _skip_rest(steps, "worker connection lost", names=step_names)

        synth_seconds = time.perf_counter() - started
        produced = Path(reply.get("wav_path") or wav_path)
        duration = float(reply.get("duration_sec") or 0.0)
        synth_ok = (
            reply.get("error_code") is None
            and produced.is_file()
            and produced.stat().st_size > 0
            and duration > 0.0
        )
        if reply.get("error_code") is not None:
            detail = f"{reply.get('error_code')}: {reply.get('message')}"
        elif not produced.is_file():
            detail = f"no file written at {produced}"
        elif duration <= 0.0:
            detail = f"duration_sec={duration} for {produced}"
        else:
            detail = f"{duration:.2f} s of audio in {synth_seconds:.2f} s -> {produced.name}"
        steps.append(_Step("synthesize", synth_ok, detail))
        if not synth_ok:
            return _skip_rest(
                steps,
                "cannot exercise cancel after a failed synthesis",
                names=step_names,
            )

        # -- stream + cancel --------------------------------------------
        # A long text, so the cancel reliably lands while audio is still coming.
        stream_text = " ".join(["conformance"] * 60)
        try:
            voice_id = None
            if kind == "clone" and ref_wav:
                # Without a reference the cloning engines never decode at all, so
                # the cancel below would acknowledge a stream that never started
                # and prove nothing. Reuse whichever voice the synthesize step used.
                for candidate in voices.list():
                    if str(voices.ref_wav_path(candidate.voice_id)) == ref_wav:
                        voice_id = candidate.voice_id
                        break
            stream_id, queue, _mode = await manager.stream(
                engine_id=engine_id,
                model_alias=alias,
                text=stream_text,
                voice_id=voice_id,
                ref_text=ref_text,
                x_vector_only=x_vector_only,
                mode=kind,
                speaker=speaker,
            )
        except (AgentVoiceError, ConnectionError, TimeoutError, OSError) as exc:
            steps.append(_Step("cancel", False, f"stream did not start: {exc}"))
            return _skip_rest(steps, "stream did not start", names=step_names)

        # Wait for real audio first: cancelling a stream that produced nothing
        # would prove nothing about a live decode.
        first_chunk = b""
        with contextlib.suppress(TimeoutError):
            first_chunk = await asyncio.wait_for(queue.get(), timeout=30.0)

        request_id = handle.stream_requests.get(stream_id, "")
        try:
            reply = await handle.connection.request(
                {
                    "id": uuid.uuid4().hex,
                    "method": "cancel",
                    "target_request_id": request_id,
                },
                timeout=10.0,
            )
        except (ConnectionError, TimeoutError, OSError) as exc:
            reply = {"error_code": "IPC_ERROR", "message": str(exc)}

        # A JOB_CANCELLED result counts as a pass: the worker acknowledged the
        # cancel by aborting the request, which is exactly the contract.
        if str(reply.get("error_code")) == "JOB_CANCELLED":
            cancel_ok = True
            detail = "JOB_CANCELLED result received"
        elif reply.get("target_request_id") == request_id and "cancelled" in reply:
            cancel_ok = True
            detail = f"cancel acknowledged, cancelled={reply.get('cancelled')}"
        else:
            cancel_ok = False
            detail = (
                f"no cancel acknowledgement: {reply.get('error_code')} "
                f"{reply.get('message', '')}"
            ).strip()
        if not first_chunk:
            detail += "; no audio arrived before the cancel"
            # Without real audio in flight, a cancel acknowledgement proves only
            # that the worker answers RPCs. Report that honestly rather than
            # letting the step pass on a no-op.
            cancel_ok = False
        steps.append(_Step("cancel", cancel_ok, detail))

        # Let the background stream task unwind and drop the engine lock before
        # shutdown, so the last step is not racing a live stream.
        task = handle.stream_task
        if task is not None and not task.done():
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=30.0)

        return await _conformance_tail(handle=handle, steps=steps, names=step_names)
    finally:
        # The shutdown step already retired this worker. Re-issuing shutdown from
        # manager.stop() would write to a closed socket and leave an un-retrieved
        # ConnectionError on the event loop, so drop the handle first.
        if handle is not None and manager._workers.get(engine_id) is handle:
            manager._workers.pop(engine_id, None)
            with contextlib.suppress(Exception):
                await handle.connection.close()
            with contextlib.suppress(Exception):
                handle.listener.close()
            if handle.process.poll() is None:
                handle.process.kill()
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(handle.process.wait, 5.0)
        with contextlib.suppress(Exception):
            await manager.stop()
        # Remove the synthetic probe voice, if one was registered, so a
        # conformance run leaves data/voices exactly as it found it.
        for leftover in voices.list():
            if leftover.name == "agent-voice-probe":
                voices.delete(leftover.voice_id)
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def _created_at(path: Path) -> float:
    """Creation time from the entry's sidecar, falling back to the file mtime."""
    sidecar = path.with_suffix(".json")
    if sidecar.is_file():
        try:
            created = json.loads(sidecar.read_text("utf-8")).get("created_at")
            if created is not None:
                return float(created)
        except (OSError, ValueError, TypeError):
            pass
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


@cache_app.command("stats")
def cache_stats() -> None:
    """Report entry count, size, cap and the oldest/newest entry as JSON."""
    from .cache import SynthesisCache

    settings = get_settings()
    cache = SynthesisCache(settings.cache_dir, max_gb=settings.max_cache_gb)

    # total_bytes() is the cache's own accounting of artifact bytes. Measuring
    # must never evict anything, so evict_lru() is deliberately not called.
    total_bytes = cache.total_bytes()

    entries: list[tuple[float, str, int]] = []
    sidecars = 0
    for path in cache.cache_dir.rglob("*"):
        if not path.is_file() or path.suffix in {".lock", ".tmp"}:
            continue
        if path.suffix == ".json":
            sidecars += 1
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        entries.append((_created_at(path), path.stem, size))

    def describe(entry: tuple[float, str, int] | None) -> dict[str, object] | None:
        if entry is None:
            return None
        created, key, size = entry
        return {
            "key": key,
            "created_at": round(created, 3),
            "created_at_iso": _iso(created),
            "size_bytes": size,
        }

    _print(
        {
            "cache_dir": str(cache.cache_dir),
            "entries": len(entries),
            "sidecars": sidecars,
            "total_bytes": total_bytes,
            "total_human": _human_bytes(total_bytes),
            "max_bytes": cache.max_bytes,
            "max_human": _human_bytes(cache.max_bytes),
            "used_percent": (
                round(100.0 * total_bytes / cache.max_bytes, 1) if cache.max_bytes else None
            ),
            "oldest_entry": describe(min(entries) if entries else None),
            "newest_entry": describe(max(entries) if entries else None),
        }
    )


@cache_app.command("clear")
def cache_clear() -> None:
    """Delete every cached artifact and sidecar, then report exactly what went."""
    from filelock import FileLock

    from .cache import SynthesisCache

    settings = get_settings()
    cache = SynthesisCache(settings.cache_dir, max_gb=settings.max_cache_gb)

    deleted: list[str] = []
    freed = 0
    # Take the same lock `put()` uses, so a concurrent write cannot interleave
    # with the delete and leave a half-written entry behind.
    with FileLock(str(cache.cache_dir / ".cache.lock")):
        for path in sorted(cache.cache_dir.rglob("*")):
            if not path.is_file() or path.suffix == ".lock":
                continue
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                continue
            freed += size
            deleted.append(str(path.relative_to(cache.cache_dir)))

    # Shard directories are clutter once empty, not data. Drop them last.
    removed_dirs = 0
    for directory in sorted(
        (path for path in cache.cache_dir.rglob("*") if path.is_dir()), reverse=True
    ):
        with contextlib.suppress(OSError):
            directory.rmdir()
            removed_dirs += 1

    _print(
        {
            "cache_dir": str(cache.cache_dir),
            "files_deleted": len(deleted),
            "directories_removed": removed_dirs,
            "bytes_freed": freed,
            "bytes_freed_human": _human_bytes(freed),
            "deleted": deleted,
        }
    )
    typer.secho(
        f"freed {_human_bytes(freed)} in {len(deleted)} file(s)", fg=typer.colors.GREEN
    )


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


@models_app.command("list")
def models_list(engine_id: str | None = typer.Option(None, help="Filter by engine.")) -> None:
    """List models with whether their weights are present locally."""
    from .engine_registry import EngineRegistry

    settings = get_settings()
    registry = EngineRegistry(settings.engines_dir)
    _print([m.model_dump(mode="json") for m in registry.list_models(engine_id)])


@models_app.command("download")
def models_download(
    engine_id: str, model_alias: str, wait: bool = typer.Option(True, help="Block until done.")
) -> None:
    """Download a model into the Hugging Face cache."""
    from huggingface_hub import snapshot_download

    from .engine_registry import EngineRegistry, weights_downloaded

    settings = get_settings()
    registry = EngineRegistry(settings.engines_dir)
    spec = registry.model_spec(engine_id, model_alias)
    manifest = registry.get(engine_id).manifest

    if spec.hf_repo.startswith("local/"):
        typer.secho(
            f"{spec.hf_repo} ships with the engine; nothing to download",
            fg=typer.colors.GREEN,
        )
        return
    # Backend-aware on purpose: Phonon stores a packed .fsq archive that the
    # plain HF cache probe does not recognise, so asking hf_model_downloaded()
    # here would re-run snapshot_download on every invocation.
    if weights_downloaded(spec, manifest):
        typer.secho(f"{spec.hf_repo} already present locally", fg=typer.colors.GREEN)
        return

    typer.echo(f"downloading {spec.hf_repo} ...")
    path = snapshot_download(repo_id=spec.hf_repo, repo_type="model")
    typer.secho(f"downloaded to {path}", fg=typer.colors.GREEN)


# ---------------------------------------------------------------------------
# voices
# ---------------------------------------------------------------------------


@voices_app.command("list")
def voices_list() -> None:
    """List registered voices."""
    from .voice_store import VoiceStore

    settings = get_settings()
    store = VoiceStore(settings.voices_dir, max_ref_text_chars=settings.max_ref_text_chars)
    _print([v.model_dump(mode="json") for v in store.list()])


@voices_app.command("add")
def voices_add(
    name: str,
    audio: Path,
    ref_text: str = typer.Option("", help="Transcript of the clip (recommended)."),
    x_vector_only: bool = typer.Option(False, help="Speaker-embedding only mode."),
) -> None:
    """Add a reference voice from a local audio file."""
    from .schemas import AgentVoiceError
    from .voice_store import VoiceStore

    settings = get_settings()
    store = VoiceStore(settings.voices_dir, max_ref_text_chars=settings.max_ref_text_chars)
    try:
        metadata = store.add(
            name=name, source_path=audio, ref_text=ref_text, x_vector_only=x_vector_only
        )
    except AgentVoiceError as exc:
        typer.secho(f"{exc.error_code}: {exc.message}", fg=typer.colors.RED, err=True)
        if exc.suggestion:
            typer.echo(exc.suggestion, err=True)
        raise typer.Exit(code=1) from exc

    _print(metadata.model_dump(mode="json"))
    for warning in metadata.warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW)


@voices_app.command("design")
def voices_design(
    name: str,
    instruct: str = typer.Option(
        ...,
        "--instruct",
        "-i",
        help="What the voice should sound like, in plain language.",
    ),
    sample_text: str = typer.Option(
        DEFAULT_DESIGN_SAMPLE_TEXT,
        "--sample-text",
        help="Sentence the designed voice speaks (5-8 s of audio is the sweet spot).",
    ),
    language: str = typer.Option(
        "English", "--language", help="Language the sample sentence is spoken in."
    ),
    save_as_clone: bool = typer.Option(
        True,
        "--save-as-clone/--no-save-as-clone",
        help="Register the render as a reusable voice (default), or keep only the preview.",
    ),
    take: int | None = typer.Option(
        None,
        "--take",
        min=1,
        help="Alternate design of the same prompt; each take caches separately.",
    ),
    candidates: int = typer.Option(
        1,
        "--candidates",
        min=1,
        max=5,
        help="How many takes to render for auditioning (1-5); the first is the preview.",
    ),
    seed: int | None = typer.Option(
        None, "--seed", help="Fixed seed, so the same prompt renders identical audio."
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace an existing voice that already has this name."
    ),
    engine: str | None = typer.Option(
        None, "--engine", help="Engine id (default: the configured engine)."
    ),
    model: str | None = typer.Option(
        None, "--model", help="Model alias; must be a voice_design checkpoint (1.7b-design)."
    ),
    quality: Literal["high", "fast"] = typer.Option(
        "high", "--quality", help="high | fast, when --model is left unset."
    ),
) -> None:
    """Design a voice from a text description, then save it as a clone.

    Runs in-process against the engines directory, so no daemon needs to be
    running. A voice_design checkpoint renders the preview; with --save-as-clone
    (the default) that preview is registered as an ordinary reference voice, which
    any cloning checkpoint can then speak with — design once, clone forever. With
    --candidates 2..5 every take is printed for auditioning; the first is the one
    that gets saved.
    """
    import asyncio

    from .api.app import AppContext
    from .schemas import AgentVoiceError, DesignVoiceRequest
    from .services.voice_design_service import render_design

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
    # Same lifecycle the daemon and the Manim demo use: start the manager, run the
    # service in-process, always stop it. Nothing here touches torch directly.
    ctx = AppContext(get_settings())

    async def run():
        await ctx.workers.start()
        try:
            return await render_design(ctx, payload)
        finally:
            await ctx.workers.stop()

    try:
        result = asyncio.run(run())
    except AgentVoiceError as exc:
        typer.secho(f"{exc.error_code}: {exc.message}", fg=typer.colors.RED, err=True)
        if exc.suggestion:
            typer.echo(exc.suggestion, err=True)
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt:
        typer.secho("interrupted", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=130) from None

    _print(result.model_dump(mode="json"))
    if result.voice_id:
        typer.secho(
            f"designed '{result.name}' -> voice {result.voice_id} "
            f"({result.duration_sec:.2f} s preview: {result.preview_file_path})",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(
            "preview only (nothing saved): " + result.preview_file_path,
            fg=typer.colors.YELLOW,
        )
    if len(result.candidates) > 1:
        typer.echo("candidates:")
        for candidate in result.candidates:
            typer.echo(
                f"  take {candidate.take}: {candidate.file_path} "
                f"({candidate.duration_sec:.2f} s)"
            )


@voices_app.command("delete")
def voices_delete(voice_id: str) -> None:
    """Delete a voice and its cached prompts."""
    from .voice_store import VoiceStore

    settings = get_settings()
    store = VoiceStore(settings.voices_dir, max_ref_text_chars=settings.max_ref_text_chars)
    if not store.delete(voice_id):
        typer.secho(f"no such voice: {voice_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho(f"deleted {voice_id}", fg=typer.colors.GREEN)


# ---------------------------------------------------------------------------
# pronunciations
# ---------------------------------------------------------------------------

# One flag definition per scope selector, so every command documents it the same
# way. `--project` and `--voice` narrow the lexicon; the merged map that a request
# actually speaks is `pronunciations show`.
ProjectOption = Annotated[
    str | None,
    typer.Option("--project", help="Scope this entry to one project slug."),
]
VoiceOption = Annotated[
    str | None,
    typer.Option("--voice", help="Scope this entry to one registered voice id."),
]

_ACRONYM = re.compile(r"\b[A-Z][A-Z0-9]{1,}\b")
"""An ALL-CAPS token of two or more characters: the acronyms a listener trips on."""

_VERSION_TOKEN = re.compile(
    r"(?<![\w.])[vV][0-9]+(?:\.[0-9]+)+(?!\w|\.\d)"
    r"|(?<![\w.])[0-9]+(?:\.[0-9]+){2,}(?!\w|\.\d)"
)
"""`v0.4.0` and `3.12.1`, mirroring `code_norm`'s version rules.

`code_candidates` deliberately omits versions — code mode rewrites them
deterministically, so they never need a lexicon entry — but `pronunciations check`
still previews them, because "what will the engine say" is the question it answers.
"""


def _scoped(call, **kwargs):
    """Call a scope resolver, turning an unresolvable scope into one clean line.

    An unknown project slug or voice id is operator input, so it gets the same
    treatment as every other CLI error: the message, the suggestion, and exit 1 —
    never a traceback.
    """
    from .schemas import AgentVoiceError

    try:
        return call(**kwargs)
    except AgentVoiceError as exc:
        typer.secho(f"{exc.error_code}: {exc.message}", fg=typer.colors.RED, err=True)
        if exc.suggestion:
            typer.echo(exc.suggestion, err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.secho(f"invalid scope: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


def _check_single_scope(project: str | None, voice: str | None) -> None:
    """Refuse `--project` + `--voice` where only one layer is being written."""
    if project is not None and voice is not None:
        typer.secho(
            "pick one scope: --project or --voice, not both",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)


def _scoped_lexicon():
    """The scope-aware lexicon: global, then project, then voice."""
    from .pronunciations import ScopedLexicon

    settings = get_settings()
    return ScopedLexicon(settings.data_dir, settings.voices_dir)


def _store_for(project: str | None, voice: str | None):
    """The PronunciationStore for one scope, or the global map when unscoped.

    `ScopedLexicon.store_for` owns the scope -> file mapping (slug validation and
    the voice-existence check), so the CLI never builds a lexicon path itself. The
    unscoped case is the operator's `data/pronunciations.json`, the same file
    `store_for(None, None)` returns, and it needs no scope resolution.
    """
    from .pronunciations import PronunciationStore

    if project is None and voice is None:
        return PronunciationStore(get_settings().pronunciations_path)
    return _scoped_lexicon().store_for(project=project, voice_id=voice)


def _rewrites(text: str, mapping: dict[str, str]) -> list[dict[str, object]]:
    """Lexicon terms that occur in `text`, longest key first, with hit counts.

    Same whole-word, case-insensitive rule the pipeline applies (see
    `pronunciations._compiled`), so the report cannot promise a rewrite that
    synthesis would not perform. `term` is the spelling found in the text — the
    actionable string for `pronunciations set` — not the merged map's lower-cased
    key. Overlapping keys are counted separately: the longest one wins at that spot,
    which is why it is listed first.
    """
    found: list[dict[str, object]] = []
    for key, spoken in sorted(mapping.items(), key=lambda item: (-len(item[0]), item[0])):
        matches = re.findall(rf"(?<!\w){re.escape(key)}(?!\w)", text, re.IGNORECASE)
        if matches:
            found.append({"term": matches[0], "spoken": spoken, "count": len(matches)})
    return found


def _unknown_acronyms(text: str, mapping: dict[str, str]) -> list[str]:
    """ALL-CAPS tokens the lexicon does not cover, in first-use order."""
    seen: dict[str, None] = {}
    for match in _ACRONYM.finditer(text):
        term = match.group(0)
        if term.lower() not in mapping:
            seen.setdefault(term, None)
    return list(seen)


def _unknown_identifiers(
    text: str, mapping: dict[str, str], acronyms: list[str]
) -> list[dict[str, str]]:
    """Code-like tokens the effective lexicon does not cover, with both readings.

    `code_norm.code_candidates` is the single definition of "code-like" in this
    project, and it takes the effective map, so the not-in-the-lexicon rule here is
    the same one synthesis applies. Each term carries how a `[spell]` span says it
    (`spelled`, via `spell_out`) and how `spell_identifiers=true` rewrites it
    (`code_mode`, via `spell_identifiers`). Terms already reported as unknown
    acronyms are left out: they are on screen once.
    """
    from .code_norm import code_candidates, spell_identifiers, spell_out
    from .pronunciations import normalize_lexicon_key

    reported = set(acronyms)
    terms = list(code_candidates(text, lexicon=mapping))
    terms.extend(match.group(0) for match in _VERSION_TOKEN.finditer(text))

    found: list[dict[str, str]] = []
    for term in dict.fromkeys(terms):
        if term in reported or normalize_lexicon_key(term) in mapping:
            continue
        found.append(
            {
                "term": term,
                "spelled": spell_out(term),
                "code_mode": spell_identifiers(term),
            }
        )
    return found


def _show_payload(*, project: str | None, voice: str | None) -> dict[str, object]:
    """Every scope layer in play, plus the merged map and its hash."""
    lexicon = _scoped_lexicon()
    layers: list[tuple[str, str | None, str | None]] = [("global", None, None)]
    if project:
        layers.append((f"project:{project}", project, None))
    if voice:
        layers.append((f"voice:{voice}", None, voice))

    scopes: list[dict[str, object]] = []
    for name, layer_project, layer_voice in layers:
        store = _store_for(layer_project, layer_voice)
        scopes.append(
            {"scope": name, "path": str(store.path), "entries": store.list()}
        )

    # Keys in the merged map are lower-cased: it is the map that reaches the
    # regex, not a copy of any one file.
    merged = lexicon.effective(project=project, voice_id=voice)
    return {
        "project": project,
        "voice_id": voice,
        "scopes": scopes,
        "merged_entries": len(merged),
        "effective": dict(sorted(merged.items())),
        "pronunciation_hash": lexicon.hash(project=project, voice_id=voice),
    }


def _check_payload(
    *,
    text: str,
    source: str,
    project: str | None,
    voice: str | None,
    spell_identifiers: bool,
) -> dict[str, object]:
    """What the lexicon would rewrite in `text` — a report, never a render."""
    lexicon = _scoped_lexicon()
    merged = lexicon.effective(project=project, voice_id=voice)
    acronyms = _unknown_acronyms(text, merged)
    payload: dict[str, object] = {
        "source": source,
        "project": project,
        "voice_id": voice,
        "characters": len(text),
        "lexicon_entries": len(merged),
        "pronunciation_hash": lexicon.hash(project=project, voice_id=voice),
        "rewritten": _rewrites(text, merged),
        "unknown_acronyms": acronyms,
    }
    if spell_identifiers:
        payload["unknown_identifiers"] = _unknown_identifiers(text, merged, acronyms)
    return payload


@pronunciations_app.command("list")
def pronunciations_list(
    project: ProjectOption = None, voice: VoiceOption = None
) -> None:
    """Print the lexicon for one scope as JSON (global when unscoped).

    `--project`/`--voice` print that one layer. The merged map a request actually
    speaks is `pronunciations show`.
    """
    _check_single_scope(project, voice)
    _print(_scoped(_store_for, project=project, voice=voice).list())


@pronunciations_app.command("set")
def pronunciations_set(
    word: str,
    replacement: str,
    project: ProjectOption = None,
    voice: VoiceOption = None,
) -> None:
    """Set one entry in the chosen scope. Matching is case-insensitive and whole-word.

    The map is read on every synthesis request, so an edit here reaches a running
    daemon immediately. The rewritten spoken text is what the cache keys on, so
    only lines that use the changed term are invalidated.
    """
    _check_single_scope(project, voice)
    store = _scoped(_store_for, project=project, voice=voice)
    try:
        entries = store.set(word, replacement)
    except ValueError as exc:
        typer.secho(f"invalid pronunciation: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    _print(entries)


@pronunciations_app.command("delete")
def pronunciations_delete(
    word: str,
    project: ProjectOption = None,
    voice: VoiceOption = None,
) -> None:
    """Remove one entry from the chosen scope. Exits non-zero when it is absent."""
    _check_single_scope(project, voice)
    store = _scoped(_store_for, project=project, voice=voice)
    try:
        deleted = store.delete(word)
    except ValueError as exc:
        typer.secho(f"invalid pronunciation: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    if not deleted:
        typer.secho(f"no such pronunciation: {word}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    _print({"deleted": word})


@pronunciations_app.command("show")
def pronunciations_show(
    project: ProjectOption = None, voice: VoiceOption = None
) -> None:
    """Print every scope, the merged map and its pronunciation_hash.

    Read-only and daemon-free: this is the exact map the next request with these
    selectors will use, so a surprising pronunciation is found without
    synthesising anything. `pronunciation_hash` is observability only — the
    synthesis cache keys on the rewritten spoken text, not on the lexicon.
    """
    _print(_scoped(_show_payload, project=project, voice=voice))


@pronunciations_app.command("check")
def pronunciations_check(
    text_or_file: str = typer.Argument(
        ..., help="Text to inspect, or the path of a UTF-8 file to read it from."
    ),
    project: ProjectOption = None,
    voice: VoiceOption = None,
    spell_identifiers: bool = typer.Option(
        False,
        "--spell-identifiers",
        help=r"Also report code-like identifiers and how \[spell] would say them.",
    ),
) -> None:
    """Predict lexicon and code-mode rewriting for a text — no synthesis, no daemon.

    The argument is read as a file when it names an existing path, otherwise it is
    treated as the text itself. The report lists the terms that will be rewritten
    (with hit counts), ALL-CAPS acronyms the effective lexicon does not cover, and
    — with `--spell-identifiers` — code-like tokens that `spell_identifiers=true`
    would expand.
    """
    path = Path(text_or_file)
    if path.is_file():
        try:
            text = path.read_text("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            typer.secho(f"cannot read {path}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        source = str(path)
    else:
        text = text_or_file
        source = "(argument)"

    _print(
        _scoped(
            _check_payload,
            text=text,
            source=source,
            project=project,
            voice=voice,
            spell_identifiers=spell_identifiers,
        )
    )


# ---------------------------------------------------------------------------
# bindings
# ---------------------------------------------------------------------------


@bindings_app.command("list")
def bindings_list() -> None:
    """List per-agent bindings."""
    from .bindings import BindingStore

    settings = get_settings()
    store = BindingStore(settings.bindings_path)
    _print([b.model_dump(mode="json") for b in store.list()])


@bindings_app.command("set")
def bindings_set(
    agent_id: str,
    voice_id: str | None = typer.Option(None),
    engine_id: str | None = typer.Option(None),
    model_alias: str | None = typer.Option(None),
) -> None:
    """Route an agent to a default voice, engine or model."""
    from .bindings import BindingStore

    settings = get_settings()
    store = BindingStore(settings.bindings_path)
    binding = store.set(
        agent_id, voice_id=voice_id, engine_id=engine_id, model_alias=model_alias
    )
    _print(binding.model_dump(mode="json"))


@bindings_app.command("delete")
def bindings_delete(agent_id: str) -> None:
    """Remove an agent binding."""
    from .bindings import BindingStore

    settings = get_settings()
    store = BindingStore(settings.bindings_path)
    deleted = store.delete(agent_id)
    _print({"deleted": agent_id if deleted else None})


if __name__ == "__main__":
    sys.exit(app())
