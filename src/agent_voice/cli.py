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

import typer

from .config import get_settings

app = typer.Typer(
    help="Headless multi-engine voice server for AI agents.", no_args_is_help=True
)
engines_app = typer.Typer(help="Inspect, install and test engines.", no_args_is_help=True)
models_app = typer.Typer(help="Inspect and download models.", no_args_is_help=True)
voices_app = typer.Typer(help="Manage reference voices.", no_args_is_help=True)
bindings_app = typer.Typer(help="Manage per-agent bindings.", no_args_is_help=True)
cache_app = typer.Typer(help="Inspect and clear the synthesis cache.", no_args_is_help=True)

app.add_typer(engines_app, name="engines")
app.add_typer(models_app, name="models")
app.add_typer(voices_app, name="voices")
app.add_typer(bindings_app, name="bindings")
app.add_typer(cache_app, name="cache")


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


def _pending_dir() -> Path:
    """Where register_engine stages a plugin awaiting operator approval."""
    return get_settings().engines_dir / ".pending"


def _staged_plugins(pending_dir: Path) -> list[Path]:
    """Directories under `.pending/` that carry an engine.yaml.

    A missing `.pending/` is normal — staging is opt-in — so it yields an empty
    list rather than an error.
    """
    if not pending_dir.is_dir():
        return []
    return sorted(
        path
        for path in pending_dir.iterdir()
        if path.is_dir() and (path / "engine.yaml").is_file()
    )


@engines_app.command("pending")
def engines_pending() -> None:
    """List plugins staged under engines/.pending/ awaiting approval."""
    pending_dir = _pending_dir()
    _print(
        [
            {"engine_id": path.name, "path": str(path)}
            for path in _staged_plugins(pending_dir)
        ]
    )


@engines_app.command("approve")
def engines_approve(engine_id: str) -> None:
    """Promote a staged plugin from engines/.pending/<id>/ to engines/<id>/."""
    import shutil

    import yaml

    from .engine_registry import EngineRegistry
    from .schemas import EngineManifest, validate_slug

    settings = get_settings()
    pending_dir = settings.engines_dir / ".pending"
    staged = pending_dir / engine_id
    destination = settings.engines_dir / engine_id

    try:
        validate_slug(engine_id)
    except ValueError as exc:
        typer.secho(f"invalid engine id {engine_id!r}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if not (staged / "engine.yaml").is_file():
        typer.secho(f"no staged plugin at {staged}", fg=typer.colors.RED, err=True)
        available = [path.name for path in _staged_plugins(pending_dir)]
        typer.echo(f"staged: {', '.join(available) if available else '(none)'}")
        raise typer.Exit(code=1)

    # Validate before moving. Promoting a manifest that cannot load would leave a
    # broken directory in engines/ that the registry silently skips.
    try:
        raw = yaml.safe_load((staged / "engine.yaml").read_text("utf-8")) or {}
        manifest = EngineManifest(**raw)
    except Exception as exc:  # noqa: BLE001 - any parse/validation failure blocks approval
        typer.secho(f"staged manifest is not usable: {exc}", fg=typer.colors.RED, err=True)
        typer.echo("Nothing was moved.", err=True)
        raise typer.Exit(code=1) from exc

    if manifest.engine_id != engine_id:
        typer.secho(
            f"manifest engine_id '{manifest.engine_id}' does not match '{engine_id}'",
            fg=typer.colors.RED,
            err=True,
        )
        typer.echo("Nothing was moved.", err=True)
        raise typer.Exit(code=1)

    if destination.exists():
        typer.secho(
            f"refusing to overwrite existing engine directory: {destination}",
            fg=typer.colors.RED,
            err=True,
        )
        typer.echo(
            "Delete or rename it first; approving never replaces a live plugin.", err=True
        )
        raise typer.Exit(code=1)

    shutil.move(str(staged), str(destination))
    # A fresh registry instance, so the reload reflects the directory on disk
    # rather than any state this process happened to hold.
    registry = EngineRegistry(settings.engines_dir)
    try:
        record = registry.get(engine_id)
    except Exception as exc:  # noqa: BLE001 - the move already happened; report it
        typer.secho(
            f"moved to {destination}, but the registry does not list it: {exc}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc

    _print(
        {
            "approved": engine_id,
            "from": str(staged),
            "to": str(destination),
            "installed": record.installed,
            "why_unavailable": record.why_unavailable,
            "engines": registry.ids(),
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
    voices = VoiceStore(settings.voices_dir)
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
        try:
            alias = registry.default_model_alias(engine_id)
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
            steps.append(
                _Step(
                    "synthesize",
                    False,
                    "engine clones voices but no reference voice is registered; run "
                    "`agent-voice voices add <name> <wav>`",
                )
            )
            return _skip_rest(steps, "cannot synthesise without a voice")

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
            stream_id, queue, _mode = await manager.stream(
                engine_id=engine_id,
                model_alias=alias,
                text=stream_text,
                voice_id=None,
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
        steps.append(_Step("cancel", cancel_ok, detail))

        # Let the background stream task unwind and drop the engine lock before
        # shutdown, so the last step is not racing a live stream.
        task = handle.stream_task
        if task is not None and not task.done():
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=30.0)

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
    store = VoiceStore(settings.voices_dir)
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
    store = VoiceStore(settings.voices_dir)
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


@voices_app.command("delete")
def voices_delete(voice_id: str) -> None:
    """Delete a voice and its cached prompts."""
    from .voice_store import VoiceStore

    settings = get_settings()
    store = VoiceStore(settings.voices_dir)
    if not store.delete(voice_id):
        typer.secho(f"no such voice: {voice_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho(f"deleted {voice_id}", fg=typer.colors.GREEN)


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
