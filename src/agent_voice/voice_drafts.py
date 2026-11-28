"""Ephemeral, quota-bounded voice drafts.

A draft is the upload/record → check/trim → commit staging area. It is not a
voice and has no engine prompt. The canonical WAV is the only file the commit
path needs; the original is retained for audit/export but is never executed or
served under its client name.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .schemas import AgentVoiceError, DraftQuality, ErrorCode, VoiceDraft


class VoiceDraftStore:
    def __init__(
        self,
        root: Path,
        *,
        ttl_seconds: float,
        max_count: int,
        max_bytes: int,
        quota_bytes: int,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self.max_count = max_count
        self.max_bytes = max_bytes
        self.quota_bytes = quota_bytes

    def dir(self, draft_id: str) -> Path:
        if len(draft_id) != 16 or any(c not in "0123456789abcdef" for c in draft_id):
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"invalid draft id: {draft_id}",
                "Use the draft_id returned by POST /v1/voice-drafts.",
            )
        return self.root / draft_id

    def audio_path(self, draft_id: str) -> Path:
        return self.dir(draft_id) / "audio.wav"

    def _meta_path(self, draft_id: str) -> Path:
        return self.dir(draft_id) / "draft.json"

    def _load(self, draft_id: str) -> dict[str, Any]:
        path = self._meta_path(draft_id)
        if not path.is_file():
            raise AgentVoiceError(
                ErrorCode.VOICE_NOT_FOUND,
                f"no such voice draft: {draft_id}",
                "Upload the sample again.",
            )
        try:
            payload = json.loads(path.read_text("utf-8"))
            expires_at = float(payload["expires_at"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise AgentVoiceError(
                ErrorCode.VOICE_NOT_FOUND,
                f"voice draft {draft_id} is unreadable",
                "Upload the sample again.",
            ) from exc
        if expires_at <= time.time():
            self.delete(draft_id)
            raise AgentVoiceError(
                ErrorCode.DRAFT_EXPIRED,
                f"voice draft {draft_id} expired",
                "Upload the sample again; drafts are intentionally temporary.",
            )
        return payload

    def get(self, draft_id: str) -> VoiceDraft:
        payload = self._load(draft_id)
        return VoiceDraft(**payload)

    def create(
        self,
        *,
        audio_wav: Path,
        original: Path,
        demuxer: str,
        quality: DraftQuality,
        peaks: list[float],
        suggested_transcript: str | None = None,
        suggested_transcript_language: str | None = None,
        suggested_transcript_engine: str | None = None,
        suggested_transcript_error: Any = None,
    ) -> VoiceDraft:
        self.prune()
        self._ensure_quota()
        draft_id = uuid.uuid4().hex[:16]
        target = self.dir(draft_id)
        target.mkdir(parents=True, exist_ok=False)
        now = time.time()
        try:
            audio_target = target / "audio.wav"
            shutil.copyfile(audio_wav, audio_target)
            original_target = target / f"original.{demuxer}"
            shutil.copyfile(original, original_target)
            draft = VoiceDraft(
                draft_id=draft_id,
                created_at=now,
                expires_at=now + self.ttl_seconds,
                duration_sec=quality.duration_sec,
                sample_rate=24_000,
                channels=1,
                peaks=peaks,
                quality=quality,
                suggested_transcript=suggested_transcript,
                suggested_transcript_language=suggested_transcript_language,
                suggested_transcript_engine=suggested_transcript_engine,
                suggested_transcript_error=suggested_transcript_error,
            )
            self._write(draft)
            return draft
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise

    def _write(self, draft: VoiceDraft) -> None:
        target = self.dir(draft.draft_id)
        fd, tmp = tempfile.mkstemp(dir=target, prefix=".draft-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(draft.model_dump(mode="json"), handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._meta_path(draft.draft_id))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def delete(self, draft_id: str) -> bool:
        target = self.dir(draft_id)
        if not target.is_dir():
            return False
        shutil.rmtree(target, ignore_errors=True)
        return True

    def _usage(self) -> tuple[int, int]:
        count = 0
        total = 0
        for path in self.root.iterdir():
            if not path.is_dir():
                continue
            count += 1
            for child in path.rglob("*"):
                if child.is_file():
                    try:
                        total += child.stat().st_size
                    except OSError:
                        pass
        return count, total

    def _ensure_quota(self) -> None:
        count, total = self._usage()
        if count >= self.max_count:
            raise AgentVoiceError(
                ErrorCode.QUOTA_EXCEEDED,
                f"{count} voice drafts are already stored; limit is {self.max_count}",
                "Commit or delete an existing draft, then retry.",
            )
        if total >= self.quota_bytes:
            raise AgentVoiceError(
                ErrorCode.QUOTA_EXCEEDED,
                "voice-draft storage quota is exhausted",
                "Delete an existing draft or raise VOICE_DRAFT_QUOTA_BYTES.",
            )

    def prune(self) -> int:
        removed = 0
        now = time.time()
        for path in self.root.iterdir():
            if not path.is_dir():
                continue
            try:
                payload = json.loads((path / "draft.json").read_text("utf-8"))
                expired = float(payload.get("expires_at", 0)) <= now
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                expired = True
            if expired:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        return removed
