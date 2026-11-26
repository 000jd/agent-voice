"""`agent-voice` command line interface."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from .config import get_settings

app = typer.Typer(
    help="Headless multi-engine voice server for AI agents.", no_args_is_help=True
)
engines_app = typer.Typer(help="Inspect and install engines.", no_args_is_help=True)
models_app = typer.Typer(help="Inspect and download models.", no_args_is_help=True)
voices_app = typer.Typer(help="Manage reference voices.", no_args_is_help=True)
bindings_app = typer.Typer(help="Manage per-agent bindings.", no_args_is_help=True)

app.add_typer(engines_app, name="engines")
app.add_typer(models_app, name="models")
app.add_typer(voices_app, name="voices")
app.add_typer(bindings_app, name="bindings")


def _print(payload) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


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
    pid_file.write_text(str(__import__("os").getpid()), "utf-8")

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
