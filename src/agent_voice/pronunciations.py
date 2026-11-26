"""Persistent pronunciation lexicon and spoken-text rewriting.

A TTS engine reads what it is given: ``kubectl`` becomes something no listener
recognises. The lexicon is a small, operator-owned replacement map persisted at
``data/pronunciations.json``; per-call overrides sit on top of it. Replacement is
case-insensitive and whole-word, so ``async`` never corrupts ``asynchronously``.

The file is read on every request rather than cached: editing the lexicon with the
CLI must affect a running daemon immediately, and the file is tiny. The canonical
hash of the effective map is part of the synthesis cache key, so a lexicon change
invalidates old audio instead of serving a stale render.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from filelock import FileLock

PRONUNCIATION_FILE = "pronunciations.json"


def _clean_mapping(value: Any) -> dict[str, str]:
    """Keep only non-empty string keys with string replacements."""
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key, replacement in value.items():
        if not isinstance(key, str) or not isinstance(replacement, str):
            continue
        word = key.strip()
        if not word:
            continue
        cleaned[word] = replacement
    return cleaned


class PronunciationStore:
    """The operator's persistent word -> spoken-form map."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = FileLock(str(self.path) + ".lock")

    def load(self) -> dict[str, str]:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return _clean_mapping(raw)

    def list(self) -> dict[str, str]:
        return dict(sorted(self.load().items(), key=lambda item: item[0].lower()))

    def _write(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".tmp-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def set(self, word: str, replacement: str) -> dict[str, str]:
        """Set one entry. Matching is case-insensitive, so re-casing replaces."""
        word = word.strip() if isinstance(word, str) else ""
        if not word:
            raise ValueError("pronunciation word must not be empty")
        if not isinstance(replacement, str):
            raise ValueError("pronunciation replacement must be a string")
        with self._lock:
            data = self.load()
            for existing in list(data):
                if existing.lower() == word.lower():
                    data.pop(existing)
            data[word] = replacement
            self._write(data)
        return self.list()

    def delete(self, word: str) -> bool:
        needle = word.strip().lower() if isinstance(word, str) else ""
        if not needle:
            return False
        with self._lock:
            data = self.load()
            matches = [key for key in data if key.lower() == needle]
            if not matches:
                return False
            for key in matches:
                data.pop(key)
            self._write(data)
        return True

    def effective(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
        """Persistent map plus per-call overrides, lower-cased for matching."""
        merged = {key.lower(): replacement for key, replacement in self.load().items()}
        for key, replacement in _clean_mapping(overrides).items():
            merged[key.lower()] = replacement
        return merged


def apply_pronunciations(text: str, mapping: dict[str, str]) -> str:
    """Replace whole-word, case-insensitive keys in ``text``.

    Keys are tried longest-first so ``kubectl pod`` wins over ``kubectl``. The
    ``(?<!\\w)``/``(?!\\w)`` guards keep ``async`` out of ``asynchronously`` while
    still matching multi-word keys and keys containing punctuation.
    """
    if not text or not mapping:
        return text
    keys = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(
        "|".join(rf"(?<!\w){re.escape(key)}(?!\w)" for key in keys),
        re.IGNORECASE,
    )
    lowered = {key.lower(): replacement for key, replacement in mapping.items()}
    return pattern.sub(lambda match: lowered[match.group(0).lower()], text)


def pronunciation_hash(mapping: dict[str, str]) -> str:
    """Deterministic hash of the effective map, for the synthesis cache key."""
    canonical = json.dumps(
        sorted((key.lower(), replacement) for key, replacement in mapping.items()),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def prepare_text(
    store: PronunciationStore | None,
    text: str,
    overrides: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Return the spoken text and the cache-key hash of the map that produced it."""
    mapping = (
        store.effective(overrides) if store is not None else _clean_mapping(overrides)
    )
    if not mapping:
        return text, pronunciation_hash({})
    return apply_pronunciations(text, mapping), pronunciation_hash(mapping)
