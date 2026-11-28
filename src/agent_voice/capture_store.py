"""Durable transcription captures: audio + transcript + timed segments."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

from .schemas import (
    AgentVoiceError,
    CaptureDetail,
    CaptureSummary,
    ErrorCode,
    TranscribeSegment,
)


class CaptureStore:
    def __init__(
        self,
        root: Path,
        *,
        max_items: int = 500,
        max_total_bytes: int = 5 * 1024**3,
        max_transcript_chars: int = 200_000,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_items = max_items
        self.max_total_bytes = max_total_bytes
        self.max_transcript_chars = max_transcript_chars

    def _dir(self, capture_id: str) -> Path:
        if len(capture_id) != 16 or any(c not in "0123456789abcdef" for c in capture_id):
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"invalid capture id: {capture_id}",
                "Use the capture_id returned by a transcription.",
            )
        return self.root / capture_id

    def audio_path(self, capture_id: str) -> Path:
        return self._dir(capture_id) / "audio.wav"

    def _meta_path(self, capture_id: str) -> Path:
        return self._dir(capture_id) / "capture.json"

    @staticmethod
    def _capture_id(media_sha256: str, engine_id: str, model_alias: str, language: str) -> str:
        payload = f"{media_sha256}:{engine_id}:{model_alias}:{language}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _read(self, capture_id: str) -> dict[str, Any]:
        path = self._meta_path(capture_id)
        if not path.is_file():
            raise AgentVoiceError(
                ErrorCode.RECORD_NOT_FOUND,
                f"no capture: {capture_id}",
                "Refresh the capture list.",
            )
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentVoiceError(
                ErrorCode.RECORD_NOT_FOUND,
                f"capture {capture_id} is unreadable",
                "Refresh the capture list.",
            ) from exc

    def _write(self, capture_id: str, payload: dict[str, Any]) -> None:
        target = self._meta_path(capture_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".capture-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def summary(self, capture_id: str) -> CaptureSummary:
        payload = self._read(capture_id)
        return CaptureSummary(
            **{k: payload[k] for k in CaptureSummary.model_fields if k in payload}
        )

    def detail(self, capture_id: str) -> CaptureDetail:
        payload = self._read(capture_id)
        segments = [TranscribeSegment(**segment) for segment in payload.get("segments", [])]
        base = {k: payload[k] for k in CaptureSummary.model_fields if k in payload}
        return CaptureDetail(**base, segments=segments, full_segments_available=True)

    def save(
        self,
        *,
        audio_path: Path,
        media_sha256: str,
        transcript: str,
        language: str,
        duration_sec: float,
        segments: list[TranscribeSegment],
        engine_id: str,
        model_alias: str,
        source: Literal["upload", "path"],
        truncated: bool = False,
        segments_omitted: int = 0,
        title: str = "",
    ) -> CaptureSummary:
        capture_id = self._capture_id(media_sha256, engine_id, model_alias, language)
        target = self._dir(capture_id)
        target.mkdir(parents=True, exist_ok=True)
        audio_target = target / "audio.wav"
        shutil.copyfile(audio_path, audio_target)
        now = time.time()
        payload = {
            "id": capture_id,
            "created_at": now,
            "title": title or "Capture",
            "transcript": transcript,
            "language": language,
            "duration_sec": round(duration_sec, 3),
            "segments_count": len(segments),
            "engine_id": engine_id,
            "model_alias": model_alias,
            "source": source,
            "audio_url": f"/v1/captures/{capture_id}/audio",
            "truncated": truncated,
            "segments_omitted": segments_omitted,
            "segments": [segment.model_dump(mode="json") for segment in segments],
            "transcript_edited": False,
        }
        self._write(capture_id, payload)
        self.prune()
        return self.summary(capture_id)

    def list(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        q: str | None = None,
    ) -> tuple[list[CaptureSummary], str | None]:
        limit = max(1, min(200, limit))
        items: list[CaptureSummary] = []
        for path in self.root.glob("*/capture.json"):
            try:
                payload = json.loads(path.read_text("utf-8"))
                items.append(
                    CaptureSummary(
                        **{k: payload[k] for k in CaptureSummary.model_fields if k in payload}
                    )
                )
            except (OSError, json.JSONDecodeError, ValueError):
                continue
        items.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        if q:
            needle = q.casefold()
            items = [
                item
                for item in items
                if needle in item.transcript.casefold() or needle in item.title.casefold()
            ]
        if cursor:
            self._dir(cursor)
            seen = False
            filtered: list[CaptureSummary] = []
            for item in items:
                if seen:
                    filtered.append(item)
                elif item.id == cursor:
                    seen = True
            if not seen:
                raise AgentVoiceError(
                    ErrorCode.BAD_REQUEST,
                    f"unknown capture cursor: {cursor}",
                    "Reload the capture list.",
                )
            items = filtered
        page = items[:limit]
        next_cursor = page[-1].id if len(items) > limit and page else None
        return page, next_cursor

    def patch(self, capture_id: str, *, transcript: str | None, title: str | None) -> CaptureDetail:
        payload = self._read(capture_id)
        if transcript is not None:
            if len(transcript) > self.max_transcript_chars:
                raise AgentVoiceError(
                    ErrorCode.TEXT_TOO_LONG,
                    f"transcript is {len(transcript)} characters, limit is "
                    f"{self.max_transcript_chars}",
                    "Shorten the transcript; the timed segments remain the source of truth.",
                )
            payload["transcript"] = transcript
            payload["transcript_edited"] = True
        if title is not None:
            title = title.strip()
            if not title or len(title) > 200:
                raise AgentVoiceError(
                    ErrorCode.BAD_REQUEST,
                    "capture title must be 1-200 characters",
                    "Use a short descriptive title.",
                )
            payload["title"] = title
        self._write(capture_id, payload)
        return self.detail(capture_id)

    def export_segments(self, capture_id: str) -> list[TranscribeSegment]:
        payload = self._read(capture_id)
        return [TranscribeSegment(**segment) for segment in payload.get("segments", [])]

    def transcript(self, capture_id: str) -> str:
        return str(self._read(capture_id).get("transcript", ""))

    def delete(self, capture_id: str) -> bool:
        target = self._dir(capture_id)
        if not target.is_dir():
            return False
        shutil.rmtree(target, ignore_errors=True)
        return True

    def count(self) -> int:
        return sum(1 for _ in self.root.glob("*/capture.json"))

    def total_bytes(self) -> int:
        total = 0
        for path in self.root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        return total

    def prune(self) -> int:
        entries: list[tuple[float, str, int]] = []
        for path in self.root.glob("*/capture.json"):
            try:
                payload = json.loads(path.read_text("utf-8"))
                size = sum(
                    child.stat().st_size for child in path.parent.rglob("*") if child.is_file()
                )
                entries.append((float(payload.get("created_at", 0)), str(payload["id"]), size))
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
        entries.sort()
        total = sum(entry[2] for entry in entries)
        removed = 0
        while entries and (len(entries) > self.max_items or total > self.max_total_bytes):
            _, capture_id, size = entries.pop(0)
            shutil.rmtree(self._dir(capture_id), ignore_errors=True)
            total -= size
            removed += 1
        return removed
