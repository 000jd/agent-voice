"""`agent-voice` command line interface.

Every command is import-light: torch lives only in the worker processes, so a
`cache stats` or `engines pending` call never touches CUDA. Engine/model/voice
imports are deferred into the command bodies for the same reason.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import typer

from .config import get_settings

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

app.add_typer(engines_app, name="engines")
app.add_typer(models_app, name="models")
app.add_typer(voices_app, name="voices")
app.add_typer(bindings_app, name="bindings")
app.add_typer(cache_app, name="cache")
app.add_typer(pronunciations_app, name="pronunciations")


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

    settings = get_settings()
    settings.ensure_dirs()

    pid_file = settings.data_dir / "daemon.pid"
    pid_file.write_text(str(os.getpid()), "utf-8")

    uvicorn.run(
        "agent_voice.api.app:create_app",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
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


def _skip_rest(steps: list[_Step], reason: str) -> list[_Step]:
    """Mark every step not yet appended as SKIP, so the table keeps its shape."""
    for name in STEP_NAMES[len(steps) :]:
        steps.append(_Step(name, None, reason))
    return steps


async def _run_conformance(*, record, registry, settings) -> list[_Step]:
    """Drive one worker: spawn → hello → load → synthesize → cancel → shutdown."""
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
            return _skip_rest(steps, "not attempted")
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
            return _skip_rest(steps, "hello handshake invalid")

        # -- load_model -------------------------------------------------
        # Conformance exercises the clone protocol; CustomVoice/VoiceDesign can
        # share the high tier, so an unfiltered pick could load a non-clone model.
        try:
            alias = registry.default_model_alias(engine_id, mode="clone")
        except AgentVoiceError as exc:
            steps.append(_Step("load_model", False, exc.message))
            return _skip_rest(steps, "no model alias to load")
        try:
            reply = await handle.connection.request(
                {"id": uuid.uuid4().hex, "method": "load_model", "model_alias": alias},
                timeout=900.0,
            )
        except (ConnectionError, TimeoutError, OSError) as exc:
            steps.append(_Step("load_model", False, f"IPC error: {exc}"))
            return _skip_rest(steps, "worker connection lost")

        load_ok = reply.get("error_code") is None and reply.get("model_alias") == alias
        detail = (
            f"alias={alias} loaded_models={reply.get('loaded_models')}"
            if reply.get("error_code") is None
            else f"{reply.get('error_code')}: {reply.get('message')}"
        )
        steps.append(_Step("load_model", load_ok, detail))
        if not load_ok:
            return _skip_rest(steps, "model failed to load")

        # -- synthesize -------------------------------------------------
        ref_wav, ref_text, x_vector_only = _reference_voice(voices)
        if not ref_wav and record.manifest.capabilities.voice_clone:
            # A cloning engine cannot synthesise without a reference, so a clean
            # checkout would fail here. Register a synthetic one for the duration
            # of the test instead of demanding manual setup.
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
            return _skip_rest(steps, "worker connection lost")

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
            return _skip_rest(steps, "cannot exercise cancel after a failed synthesis")

        # -- stream + cancel --------------------------------------------
        # A long text, so the cancel reliably lands while audio is still coming.
        stream_text = " ".join(["conformance"] * 60)
        try:
            voice_id = None
            if ref_wav:
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
            )
        except (AgentVoiceError, ConnectionError, TimeoutError, OSError) as exc:
            steps.append(_Step("cancel", False, f"stream did not start: {exc}"))
            return _skip_rest(steps, "stream did not start")

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

        # -- health -----------------------------------------------------
        # The protocol's health RPC is what a manager uses to check a live
        # worker, so a conformance run that never calls it is not exercising v1.
        try:
            reply = await handle.connection.request(
                {"id": uuid.uuid4().hex, "method": "health"}, timeout=15.0
            )
        except (ConnectionError, TimeoutError, OSError) as exc:
            steps.append(_Step("health", False, f"no reply: {exc}"))
            return _skip_rest(steps, "worker did not answer health")

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

    from .engine_registry import EngineRegistry, hf_model_downloaded

    settings = get_settings()
    registry = EngineRegistry(settings.engines_dir)
    spec = registry.model_spec(engine_id, model_alias)

    if hf_model_downloaded(spec.hf_repo):
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


@pronunciations_app.command("list")
def pronunciations_list() -> None:
    """Print the persistent word -> spoken-form map as JSON."""
    from .pronunciations import PronunciationStore

    store = PronunciationStore(get_settings().pronunciations_path)
    _print(store.list())


@pronunciations_app.command("set")
def pronunciations_set(word: str, replacement: str) -> None:
    """Set one entry. Matching is case-insensitive and whole-word.

    The map is read on every synthesis request, so an edit here reaches a running
    daemon immediately. The rewritten spoken text is what the cache keys on, so
    only lines that use the changed term are invalidated.
    """
    from .pronunciations import PronunciationStore

    store = PronunciationStore(get_settings().pronunciations_path)
    try:
        entries = store.set(word, replacement)
    except ValueError as exc:
        typer.secho(f"invalid pronunciation: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    _print(entries)


@pronunciations_app.command("delete")
def pronunciations_delete(word: str) -> None:
    """Remove one entry. Exits non-zero when the word is not in the lexicon."""
    from .pronunciations import PronunciationStore

    store = PronunciationStore(get_settings().pronunciations_path)
    try:
        deleted = store.delete(word)
    except ValueError as exc:
        typer.secho(f"invalid pronunciation: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    if not deleted:
        typer.secho(f"no such pronunciation: {word}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    _print({"deleted": word})


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
