"""Durable generation history.

One atomic JSON record per generation, following the job store's layout. The
record stores the original request privately so a rerender can reproduce the
call, but the public contract exposes only stable, user-meaningful fields.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from .schemas import AgentVoiceError, ErrorCode, HistoryItem

HistoryKind = Literal["speak", "script", "design", "transcribe"]


class HistoryStore:
    def __init__(self, root: Path, *, max_items: int = 2000) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_items = max_items

    def _path(self, entry_id: str) -> Path:
        if len(entry_id) != 16 or any(c not in "0123456789abcdef" for c in entry_id):
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"invalid history id: {entry_id}",
                "Use the id returned by GET /v1/history.",
            )
        return self.root / f"{entry_id}.json"

    def _read(self, path: Path) -> tuple[HistoryItem, dict[str, Any]] | None:
        try:
            payload = json.loads(path.read_text("utf-8"))
            item = HistoryItem(**{k: v for k, v in payload.items() if k != "request"})
            request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
            return item, request
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def _write(self, item: HistoryItem, request: dict[str, Any]) -> None:
        path = self._path(item.id)
        body = {**item.model_dump(mode="json"), "request": request}
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".history-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def record(
        self,
        *,
        kind: HistoryKind,
        text: str = "",
        status: Literal["succeeded", "failed"] = "succeeded",
        voice_id: str | None = None,
        voice_name: str = "",
        speaker: str | None = None,
        engine_id: str = "",
        model_alias: str = "",
        device: str | None = None,
        seed: int | None = None,
        take: int | None = None,
        duration_sec: float = 0.0,
        format: str = "wav",
        artifact_id: str | None = None,
        audio_url: str = "",
        capture_id: str | None = None,
        rerenderable: bool = False,
        error: Any = None,
        request: dict[str, Any] | None = None,
    ) -> HistoryItem:
        item = HistoryItem(
            id=uuid.uuid4().hex[:16],
            created_at=time.time(),
            kind=kind,
            status=status,
            text=text,
            voice_id=voice_id,
            voice_name=voice_name,
            speaker=speaker,
            engine_id=engine_id,
            model_alias=model_alias,
            device=device,
            seed=seed,
            take=take,
            duration_sec=duration_sec,
            format=format,
            artifact_id=artifact_id,
            audio_url=audio_url,
            capture_id=capture_id,
            rerenderable=rerenderable,
            error=error,
        )
        self._write(item, request or {})
        self.prune()
        return item

    def get(self, entry_id: str) -> HistoryItem:
        found = self._read(self._path(entry_id))
        if found is None:
            raise AgentVoiceError(
                ErrorCode.RECORD_NOT_FOUND,
                f"no history entry: {entry_id}",
                "Refresh the library; the record may have been pruned.",
            )
        return found[0]

    def request(self, entry_id: str) -> dict[str, Any]:
        found = self._read(self._path(entry_id))
        if found is None:
            raise AgentVoiceError(
                ErrorCode.RECORD_NOT_FOUND,
                f"no history entry: {entry_id}",
                "Refresh the library.",
            )
        return found[1]

    def delete(self, entry_id: str) -> bool:
        path = self._path(entry_id)
        if not path.is_file():
            return False
        path.unlink(missing_ok=True)
        return True

    def toggle_favorite(self, entry_id: str) -> HistoryItem:
        found = self._read(self._path(entry_id))
        if found is None:
            raise AgentVoiceError(
                ErrorCode.RECORD_NOT_FOUND,
                f"no history entry: {entry_id}",
                "Refresh the library.",
            )
        item, request = found
        item.favorite = not item.favorite
        self._write(item, request)
        return item

    def list(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        kind: str | None = None,
        voice: str | None = None,
        favorite: bool | None = None,
        q: str | None = None,
    ) -> tuple[list[HistoryItem], str | None]:
        limit = max(1, min(200, limit))
        items: list[HistoryItem] = []
        for path in self.root.glob("*.json"):
            found = self._read(path)
            if found is not None:
                items.append(found[0])
        items.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        if kind:
            items = [item for item in items if item.kind == kind]
        if voice:
            items = [item for item in items if item.voice_id == voice or item.voice_name == voice]
        if favorite is not None:
            items = [item for item in items if item.favorite is favorite]
        if q:
            needle = q.casefold()
            items = [
                item
                for item in items
                if needle in item.text.casefold()
                or needle in item.voice_name.casefold()
                or needle in item.model_alias.casefold()
            ]
        if cursor:
            self._path(cursor)  # validate before searching
            seen = False
            filtered: list[HistoryItem] = []
            for item in items:
                if seen:
                    filtered.append(item)
                elif item.id == cursor:
                    seen = True
            if not seen:
                raise AgentVoiceError(
                    ErrorCode.BAD_REQUEST,
                    f"unknown history cursor: {cursor}",
                    "Reload the library and use the returned next_cursor.",
                )
            items = filtered
        page = items[:limit]
        next_cursor = page[-1].id if len(items) > limit and page else None
        return page, next_cursor

    def count(self) -> int:
        return sum(1 for _ in self.root.glob("*.json"))

    def prune(self) -> int:
        items: list[HistoryItem] = []
        for path in self.root.glob("*.json"):
            found = self._read(path)
            if found is not None:
                items.append(found[0])
        if len(items) <= self.max_items:
            return 0
        # Keep favourites where possible; if favourites alone exceed the cap,
        # oldest favourites still have to go.
        items.sort(key=lambda item: (item.favorite, item.created_at), reverse=False)
        removed = 0
        for item in items[: len(items) - self.max_items]:
            self._path(item.id).unlink(missing_ok=True)
            removed += 1
        return removed
