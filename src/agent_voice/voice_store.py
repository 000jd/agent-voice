"""Engine-agnostic voice store plus per-engine prompt cache.

Reference audio is normalised once (24 kHz mono, silence-trimmed) and content
addressed by SHA-256. Prompt caches are keyed by ``(engine, model)`` and
invalidated whenever the reference audio, its transcript, or the backend version
changes — a stale prompt silently produces the wrong speaker, which is worse
than recomputing.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from filelock import FileLock

from .audio_utils import canonicalise_ref_audio
from .schemas import AgentVoiceError, ErrorCode, VoiceMetadata
from .security import (
    MAX_REF_AUDIO_SECONDS,
    MIN_REF_AUDIO_SECONDS,
    SWEET_SPOT_SECONDS,
)

METADATA_NAME = "metadata.json"
REF_WAV_NAME = "ref.wav"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class VoiceStore:
    def __init__(self, voices_dir: Path) -> None:
        self.voices_dir = Path(voices_dir)
        self.voices_dir.mkdir(parents=True, exist_ok=True)

    # -- lookup ---------------------------------------------------------
    def voice_dir(self, voice_id: str) -> Path:
        return self.voices_dir / voice_id

    def ref_wav_path(self, voice_id: str) -> Path:
        return self.voice_dir(voice_id) / REF_WAV_NAME

    def exists(self, voice_id: str) -> bool:
        return (self.voice_dir(voice_id) / METADATA_NAME).is_file()

    def get(self, voice_id: str) -> VoiceMetadata:
        path = self.voice_dir(voice_id) / METADATA_NAME
        if not path.is_file():
            raise AgentVoiceError(
                ErrorCode.VOICE_NOT_FOUND,
                f"no such voice: {voice_id}",
                "Call list_voices() to see available voices, or clone_voice() first.",
            )
        return VoiceMetadata(**json.loads(path.read_text("utf-8")))

    def list(self) -> list[VoiceMetadata]:
        voices: list[VoiceMetadata] = []
        for entry in sorted(self.voices_dir.iterdir()):
            if not entry.is_dir():
                continue
            meta_path = entry / METADATA_NAME
            if not meta_path.is_file():
                continue
            try:
                voices.append(VoiceMetadata(**json.loads(meta_path.read_text("utf-8"))))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return voices

    def find_by_name(self, name: str) -> VoiceMetadata | None:
        for voice in self.list():
            if voice.name == name or voice.voice_id == name:
                return voice
        return None

    def resolve(self, name_or_id: str | None) -> VoiceMetadata | None:
        if not name_or_id:
            return None
        if self.exists(name_or_id):
            return self.get(name_or_id)
        return self.find_by_name(name_or_id)

    # -- mutation -------------------------------------------------------
    def add(
        self,
        *,
        name: str,
        source_path: Path,
        ref_text: str = "",
        x_vector_only: bool = False,
        overwrite: bool = False,
    ) -> VoiceMetadata:
        """Register (or idempotently re-return) a cloned voice.

        The incoming clip is canonicalised into a staging directory first, because
        only the canonical bytes tell us whether this is a retry of an existing
        voice or a genuine name clash. A rejected clone therefore never leaves a
        half-built voice directory behind.
        """
        source = Path(source_path)
        if not source.is_file():
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"reference audio not found: {source_path}",
                "Pass an existing local audio file.",
            )

        staging = Path(tempfile.mkdtemp(dir=str(self.voices_dir), prefix=".staging-"))
        try:
            staged_wav = staging / REF_WAV_NAME
            duration = canonicalise_ref_audio(source, staged_wav)

            if duration < MIN_REF_AUDIO_SECONDS:
                raise AgentVoiceError(
                    ErrorCode.INVALID_REF_AUDIO,
                    f"reference audio is {duration:.2f}s, minimum is "
                    f"{MIN_REF_AUDIO_SECONDS:.0f}s",
                    "Provide a clip between 3 and 10 seconds of clean speech.",
                )
            if duration > MAX_REF_AUDIO_SECONDS:
                raise AgentVoiceError(
                    ErrorCode.INVALID_REF_AUDIO,
                    f"reference audio is {duration:.2f}s, maximum is "
                    f"{MAX_REF_AUDIO_SECONDS:.0f}s",
                    "Trim to a 3-10 second clip; longer clips waste VRAM without helping.",
                )

            digest = sha256_file(staged_wav)
            existing = next((voice for voice in self.list() if voice.name == name), None)
            if existing is not None:
                identical = (
                    existing.ref_wav_sha256 == digest
                    and existing.ref_text == ref_text
                    and existing.x_vector_only_mode == x_vector_only
                )
                if identical:
                    # Agent retries land here: same name, same audio, same
                    # transcript. Returning the existing voice keeps clone_voice
                    # idempotent instead of piling up duplicates.
                    return existing
                if not overwrite:
                    raise AgentVoiceError(
                        ErrorCode.BAD_REQUEST,
                        f"a voice named '{name}' already exists (id {existing.voice_id}) "
                        "with different audio or ref_text",
                        "Pass overwrite=True (or ?overwrite=true on POST /v1/voices) to "
                        "replace it, or choose a different name.",
                    )
                # Drop the old directory with its prompt caches: a stale prompt for
                # replaced audio would silently produce the previous speaker.
                self.delete(existing.voice_id)

            warnings: list[str] = []
            low, high = SWEET_SPOT_SECONDS
            if not (low <= duration <= high):
                warnings.append(
                    f"reference is {duration:.1f}s; 3-10s is the quality sweet spot"
                )
            if not x_vector_only and not ref_text.strip():
                warnings.append(
                    "ref_text is empty in ICL mode; cloning quality drops noticeably. "
                    "Either transcribe the clip or set x_vector_only=true."
                )

            voice_id = uuid.uuid4().hex[:12]
            target_dir = self.voice_dir(voice_id)
            target_dir.mkdir(parents=True, exist_ok=False)
            os.replace(staged_wav, target_dir / REF_WAV_NAME)

            metadata = VoiceMetadata(
                voice_id=voice_id,
                name=name,
                ref_wav_sha256=digest,
                ref_text=ref_text,
                x_vector_only_mode=x_vector_only,
                duration_sec=round(duration, 3),
                created_at=time.time(),
                warnings=warnings,
            )
            _atomic_write_json(target_dir / METADATA_NAME, metadata.model_dump())
            return metadata
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def delete(self, voice_id: str) -> bool:
        target = self.voice_dir(voice_id)
        if not target.is_dir():
            return False
        shutil.rmtree(target, ignore_errors=True)
        return True

    # -- prompt cache ---------------------------------------------------
    def prompt_cache_paths(
        self, voice_id: str, engine_id: str, model_alias: str
    ) -> tuple[Path, Path]:
        base = self.voice_dir(voice_id) / "cache" / engine_id / model_alias
        return base / "prompt.pt", base / "prompt_meta.json"

    def load_prompt(
        self,
        *,
        voice_id: str,
        engine_id: str,
        model_alias: str,
        ref_wav_sha256: str,
        ref_text: str,
        backend_version: str,
    ) -> Path | None:
        """Return the cached prompt path if it is still valid, else None."""
        prompt_path, meta_path = self.prompt_cache_paths(voice_id, engine_id, model_alias)
        if not prompt_path.is_file() or not meta_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        valid = (
            meta.get("ref_wav_sha256") == ref_wav_sha256
            and meta.get("ref_text") == ref_text
            and meta.get("backend_version") == backend_version
        )
        if not valid:
            prompt_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            return None
        return prompt_path

    def invalidate_prompts(self, voice_id: str) -> int:
        removed = 0
        cache_root = self.voice_dir(voice_id) / "cache"
        if cache_root.is_dir():
            for prompt in cache_root.rglob("prompt.pt"):
                prompt.unlink(missing_ok=True)
                removed += 1
            for meta in cache_root.rglob("prompt_meta.json"):
                meta.unlink(missing_ok=True)
        return removed


class PromptCacheWriter:
    """Serialises a worker-side prompt into the store.

    The worker writes ``prompt.pt`` as a plain dict of tensors and strings so it
    can be reloaded with ``torch.load(weights_only=True)``. It also writes the
    metadata sidecar, because only the worker knows the exact backend version.
    """

    def __init__(self, store: VoiceStore) -> None:
        self.store = store

    def record(
        self,
        *,
        voice_id: str,
        engine_id: str,
        model_alias: str,
        prompt_path: Path,
        ref_wav_sha256: str,
        ref_text: str,
        backend_version: str,
    ) -> None:
        _, meta_path = self.store.prompt_cache_paths(voice_id, engine_id, model_alias)
        _atomic_write_json(
            meta_path,
            {
                "ref_wav_sha256": ref_wav_sha256,
                "ref_text": ref_text,
                "backend_version": backend_version,
                "engine_id": engine_id,
                "model_alias": model_alias,
                "prompt_path": str(prompt_path),
                "created_at": time.time(),
            },
        )


def prompt_lock(voice_id: str, engine_id: str, model_alias: str, voices_dir: Path) -> FileLock:
    """Serialise prompt computation for one (voice, engine, model) triple."""
    path = Path(voices_dir) / voice_id / "cache" / engine_id / model_alias
    path.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path / ".prompt.lock"))
