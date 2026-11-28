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
from typing import Any, Literal

from filelock import FileLock

from .audio_utils import canonicalise_ref_audio, probe_duration
from .ids import validate_hex_id, validate_resource_id
from .schemas import AgentVoiceError, ErrorCode, VoiceMetadata, VoiceSample, validate_voice_name
from .security import (
    MAX_REF_AUDIO_SECONDS,
    MIN_REF_AUDIO_SECONDS,
    SWEET_SPOT_SECONDS,
)

METADATA_NAME = "metadata.json"
REF_WAV_NAME = "ref.wav"
SAMPLES_DIR = "samples"
PRIMARY_SAMPLE_ID = "primary"
LEXICON_NAME = "pronunciations.json"
"""A voice's own scoped lexicon, beside ``metadata.json`` in its directory.

``ScopedLexicon`` reads this file; ``pronunciations.py`` imports the name from
here (lazily, to avoid a cycle) so there is exactly one definition of it. Deleting
a voice removes its lexicon with the rest of the directory — the lexicon belongs
to the voice and outliving it would resurrect it on a reused id.
"""


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
    def __init__(self, voices_dir: Path, max_ref_text_chars: int = 2000) -> None:
        self.voices_dir = Path(voices_dir)
        self.voices_dir.mkdir(parents=True, exist_ok=True)
        self.max_ref_text_chars = max_ref_text_chars
        """Longest accepted transcript, from MAX_REF_TEXT_CHARS.

        A transcript far longer than the clip cannot describe it: the caller has
        pasted a whole document, and the fix is a shorter reference, not more text.
        """

    # -- lookup ---------------------------------------------------------
    def voice_dir(self, voice_id: str) -> Path:
        validate_resource_id(
            voice_id,
            noun="voice id",
            hint="Use a voice_id returned by GET /v1/voices.",
        )
        return self.voices_dir / voice_id

    def ref_wav_path(self, voice_id: str) -> Path:
        return self.voice_dir(voice_id) / REF_WAV_NAME

    def lexicon_path(self, voice_id: str) -> Path:
        """Where this voice's scoped pronunciation lexicon lives (may not exist).

        A path only: writing one is the operator's/CLI's call, and the file is
        optional — a voice without a lexicon simply contributes nothing to the
        spoken-text merge.
        """
        return self.voice_dir(voice_id) / LEXICON_NAME

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
        try:
            if self.exists(name_or_id):
                return self.get(name_or_id)
        except AgentVoiceError:
            # A human-facing name may contain spaces; it is resolved below by
            # scanning metadata, never by joining it into a path.
            pass
        return self.find_by_name(name_or_id)

    # -- samples --------------------------------------------------------
    def sample_path(self, voice_id: str, sample_id: str) -> Path:
        if sample_id != PRIMARY_SAMPLE_ID:
            validate_hex_id(
                sample_id,
                noun="sample id",
                hint="Use the sample_id returned by the voice endpoint.",
            )
        return self.voice_dir(voice_id) / SAMPLES_DIR / f"{sample_id}.wav"

    def samples_for(self, voice: VoiceMetadata) -> list[VoiceSample]:
        if voice.samples:
            return voice.samples
        return [
            VoiceSample(
                sample_id=PRIMARY_SAMPLE_ID,
                duration_sec=voice.duration_sec,
                ref_text=voice.ref_text,
                is_primary=True,
                created_at=voice.created_at,
            )
        ]

    def _write_metadata(self, metadata: VoiceMetadata) -> None:
        _atomic_write_json(self.voice_dir(metadata.voice_id) / METADATA_NAME, metadata.model_dump())

    # -- mutation -------------------------------------------------------
    def _validate_inputs(self, name: str, ref_text: str, notes: str, language: str) -> str:
        try:
            name = validate_voice_name(name)
        except ValueError as exc:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                str(exc),
                "Use 1-64 letters, digits, spaces, dots, hyphens or underscores.",
            ) from exc
        if len(ref_text) > self.max_ref_text_chars:
            raise AgentVoiceError(
                ErrorCode.TEXT_TOO_LONG,
                f"ref_text is {len(ref_text)} characters, limit is {self.max_ref_text_chars}",
                "Shorten the transcript; 3-10 s of speech is the clone sweet spot.",
            )
        if len(notes) > 2000:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                "notes are longer than 2000 characters",
                "Keep notes short and factual.",
            )
        if not language.strip() or len(language) > 64:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                "language must be 1-64 characters",
                "Use a language name or leave it as Auto.",
            )
        return name

    def _finalize(
        self,
        *,
        staged_wav: Path,
        duration: float,
        name: str,
        ref_text: str,
        x_vector_only: bool,
        overwrite: bool,
        source: Literal["cloned", "designed"],
        design_instruct: str,
        language: str,
        notes: str,
        consent: bool,
    ) -> VoiceMetadata:
        if duration < MIN_REF_AUDIO_SECONDS:
            raise AgentVoiceError(
                ErrorCode.INVALID_REF_AUDIO,
                f"reference audio is {duration:.2f}s, minimum is {MIN_REF_AUDIO_SECONDS:.0f}s",
                "Provide a clip between 3 and 10 seconds of clean speech.",
            )
        if duration > MAX_REF_AUDIO_SECONDS:
            raise AgentVoiceError(
                ErrorCode.INVALID_REF_AUDIO,
                f"reference audio is {duration:.2f}s, maximum is {MAX_REF_AUDIO_SECONDS:.0f}s",
                "Trim to a 3-10 second clip; longer clips waste VRAM without helping.",
            )

        digest = sha256_file(staged_wav)
        existing = next((voice for voice in self.list() if voice.name == name), None)
        if existing is not None:
            identical = (
                existing.ref_wav_sha256 == digest
                and existing.ref_text == ref_text
                and existing.x_vector_only_mode == x_vector_only
                and existing.source == source
                and existing.design_instruct == design_instruct
            )
            if identical:
                return existing
            if not overwrite:
                raise AgentVoiceError(
                    ErrorCode.BAD_REQUEST,
                    f"a voice named '{name}' already exists (id {existing.voice_id}) "
                    "with different audio, ref_text or source",
                    "Pass overwrite=True (or ?overwrite=true on POST /v1/voices) to "
                    "replace it, or choose a different name.",
                )
            self.delete(existing.voice_id)

        warnings: list[str] = []
        low, high = SWEET_SPOT_SECONDS
        if not (low <= duration <= high):
            warnings.append(f"reference is {duration:.1f}s; 3-10s is the quality sweet spot")
        if not x_vector_only and not ref_text.strip():
            warnings.append(
                "ref_text is empty in ICL mode; cloning quality drops noticeably. "
                "Either transcribe the clip or set x_vector_only=true."
            )

        voice_id = uuid.uuid4().hex[:12]
        target_dir = self.voice_dir(voice_id)
        (target_dir / SAMPLES_DIR).mkdir(parents=True, exist_ok=False)
        sample_id = uuid.uuid4().hex[:16]
        sample_path = target_dir / SAMPLES_DIR / f"{sample_id}.wav"
        shutil.copyfile(staged_wav, sample_path)
        os.replace(staged_wav, target_dir / REF_WAV_NAME)
        now = time.time()
        metadata = VoiceMetadata(
            voice_id=voice_id,
            name=name,
            ref_wav_sha256=digest,
            ref_text=ref_text,
            x_vector_only_mode=x_vector_only,
            duration_sec=round(duration, 3),
            created_at=now,
            warnings=warnings,
            source=source,
            design_instruct=design_instruct,
            language=language,
            notes=notes,
            samples=[
                VoiceSample(
                    sample_id=sample_id,
                    duration_sec=round(duration, 3),
                    ref_text=ref_text,
                    is_primary=True,
                    created_at=now,
                )
            ],
            consent_recorded_at=now if consent else None,
            consent_version="responsible-use-v1" if consent else "",
        )
        self._write_metadata(metadata)
        return metadata

    def add(
        self,
        *,
        name: str,
        source_path: Path,
        ref_text: str = "",
        x_vector_only: bool = False,
        overwrite: bool = False,
        source: Literal["cloned", "designed"] = "cloned",
        design_instruct: str = "",
        language: str = "Auto",
        notes: str = "",
        consent: bool = False,
    ) -> VoiceMetadata:
        """Canonicalise an existing file, then register it as a voice."""
        name = self._validate_inputs(name, ref_text, notes, language)
        clip = Path(source_path)
        if not clip.is_file():
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"reference audio not found: {source_path}",
                "Pass an existing local audio file.",
            )
        staging = Path(tempfile.mkdtemp(dir=str(self.voices_dir), prefix=".staging-"))
        try:
            staged_wav = staging / REF_WAV_NAME
            duration = canonicalise_ref_audio(clip, staged_wav)
            return self._finalize(
                staged_wav=staged_wav,
                duration=duration,
                name=name,
                ref_text=ref_text,
                x_vector_only=x_vector_only,
                overwrite=overwrite,
                source=source,
                design_instruct=design_instruct,
                language=language,
                notes=notes,
                consent=consent,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def add_prepared(
        self,
        *,
        name: str,
        prepared_wav: Path,
        ref_text: str = "",
        x_vector_only: bool = False,
        overwrite: bool = False,
        source: Literal["cloned", "designed"] = "cloned",
        design_instruct: str = "",
        language: str = "Auto",
        notes: str = "",
        consent: bool = False,
    ) -> VoiceMetadata:
        """Register a canonical 24 kHz mono WAV produced by draft/trim."""
        name = self._validate_inputs(name, ref_text, notes, language)
        prepared = Path(prepared_wav)
        if not prepared.is_file():
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"prepared reference audio not found: {prepared}",
                "Re-upload the sample and retry.",
            )
        staging = Path(tempfile.mkdtemp(dir=str(self.voices_dir), prefix=".staging-"))
        try:
            staged_wav = staging / REF_WAV_NAME
            shutil.copyfile(prepared, staged_wav)
            duration = probe_duration(staged_wav)
            return self._finalize(
                staged_wav=staged_wav,
                duration=duration,
                name=name,
                ref_text=ref_text,
                x_vector_only=x_vector_only,
                overwrite=overwrite,
                source=source,
                design_instruct=design_instruct,
                language=language,
                notes=notes,
                consent=consent,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def add_sample(
        self,
        voice_id: str,
        *,
        prepared_wav: Path,
        ref_text: str = "",
        make_primary: bool = False,
    ) -> VoiceMetadata:
        metadata = self.get(voice_id)
        if ref_text and len(ref_text) > self.max_ref_text_chars:
            raise AgentVoiceError(
                ErrorCode.TEXT_TOO_LONG,
                f"ref_text is {len(ref_text)} characters, limit is {self.max_ref_text_chars}",
                "Shorten the transcript.",
            )
        path = Path(prepared_wav)
        if not path.is_file():
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                "prepared sample not found",
                "Re-upload the sample and retry.",
            )
        staging = Path(tempfile.mkdtemp(dir=str(self.voices_dir), prefix=".sample-"))
        try:
            canonical = staging / REF_WAV_NAME
            duration = canonicalise_ref_audio(path, canonical)
            if not MIN_REF_AUDIO_SECONDS <= duration <= MAX_REF_AUDIO_SECONDS:
                raise AgentVoiceError(
                    ErrorCode.INVALID_REF_AUDIO,
                    f"sample is {duration:.2f}s; expected 1-30s",
                    "Trim the sample before adding it.",
                )
            samples = list(self.samples_for(metadata))
            sample_id = uuid.uuid4().hex[:16]
            target = self.voice_dir(voice_id) / SAMPLES_DIR / f"{sample_id}.wav"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(canonical, target)
            samples.append(
                VoiceSample(
                    sample_id=sample_id,
                    duration_sec=round(duration, 3),
                    ref_text=ref_text,
                    is_primary=False,
                    created_at=time.time(),
                )
            )
            metadata.samples = samples
            self._write_metadata(metadata)
            if make_primary:
                return self.promote_sample(voice_id, sample_id)
            return metadata
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def promote_sample(self, voice_id: str, sample_id: str) -> VoiceMetadata:
        metadata = self.get(voice_id)
        samples = list(self.samples_for(metadata))
        chosen = next((sample for sample in samples if sample.sample_id == sample_id), None)
        if chosen is None:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"no such sample: {sample_id}",
                "List the voice to see its samples.",
            )
        source = self.sample_path(voice_id, sample_id)
        if source.is_file():
            shutil.copyfile(source, self.ref_wav_path(voice_id))
        metadata.ref_wav_sha256 = sha256_file(self.ref_wav_path(voice_id))
        metadata.ref_text = chosen.ref_text
        metadata.duration_sec = chosen.duration_sec
        metadata.samples = [
            sample.model_copy(update={"is_primary": sample.sample_id == sample_id})
            for sample in samples
        ]
        self.invalidate_prompts(voice_id)
        self._write_metadata(metadata)
        return metadata

    def remove_sample(self, voice_id: str, sample_id: str) -> VoiceMetadata:
        metadata = self.get(voice_id)
        samples = list(self.samples_for(metadata))
        if len(samples) <= 1:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                "a voice must keep at least one sample",
                "Delete the voice instead, or add another sample first.",
            )
        chosen = next((sample for sample in samples if sample.sample_id == sample_id), None)
        if chosen is None:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"no such sample: {sample_id}",
                "List the voice to see its samples.",
            )
        remaining = [sample for sample in samples if sample.sample_id != sample_id]
        path = self.voice_dir(voice_id) / SAMPLES_DIR / f"{sample_id}.wav"
        path.unlink(missing_ok=True)
        metadata.samples = remaining
        if chosen.is_primary:
            self._write_metadata(metadata)
            self.promote_sample(voice_id, remaining[0].sample_id)
        else:
            self._write_metadata(metadata)
        return self.get(voice_id)

    def update(
        self,
        voice_id: str,
        *,
        name: str | None = None,
        notes: str | None = None,
        language: str | None = None,
    ) -> VoiceMetadata:
        metadata = self.get(voice_id)
        if name is not None:
            name = self._validate_inputs(name, metadata.ref_text, notes or "", language or "Auto")
            if name != metadata.name and any(
                voice.name == name and voice.voice_id != voice_id for voice in self.list()
            ):
                raise AgentVoiceError(
                    ErrorCode.BAD_REQUEST,
                    f"a voice named '{name}' already exists",
                    "Choose another name.",
                )
            metadata.name = name
        if notes is not None:
            self._validate_inputs(
                metadata.name, metadata.ref_text, notes, language or metadata.language
            )
            metadata.notes = notes
        if language is not None:
            self._validate_inputs(metadata.name, metadata.ref_text, "", language)
            metadata.language = language
        self._write_metadata(metadata)
        return metadata

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
