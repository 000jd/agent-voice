"""Persistent pronunciation lexicon and spoken-text rewriting.

A TTS engine reads what it is given: ``kubectl`` becomes something no listener
recognises. The lexicon is a small, operator-owned replacement map persisted at
``data/pronunciations.json``; per-call overrides sit on top of it. Replacement is
case-insensitive and whole-word, so ``async`` never corrupts ``asynchronously``.

The rewritten spoken text is what the synthesis pipeline speaks and what the
synthesis cache keys on: adding an unrelated entry leaves existing renders cached,
while changing a term that appears in a line changes that line's key. Subtitles
keep the original written text.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from filelock import FileLock

PRONUNCIATION_FILE = "pronunciations.json"
MAX_LEXICON_ENTRIES = 500
MAX_LEXICON_TERM_CHARS = 200


def validate_pronunciation_map(mapping: dict[str, str] | None) -> None:
    """Reject an oversized lexicon before it reaches the regex or a worker."""
    if not mapping:
        return
    if len(mapping) > MAX_LEXICON_ENTRIES:
        raise ValueError(
            f"pronunciation map has {len(mapping)} entries, limit is {MAX_LEXICON_ENTRIES}"
        )
    for word, replacement in mapping.items():
        if not isinstance(word, str) or not isinstance(replacement, str):
            raise ValueError("pronunciation keys and replacements must be strings")
        if len(word) > MAX_LEXICON_TERM_CHARS:
            raise ValueError(
                f"pronunciation key is {len(word)} characters, limit is "
                f"{MAX_LEXICON_TERM_CHARS}"
            )
        if len(replacement) > MAX_LEXICON_TERM_CHARS:
            raise ValueError(
                f"pronunciation replacement for {word!r} is {len(replacement)} "
                f"characters, limit is {MAX_LEXICON_TERM_CHARS}"
            )


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
        validate_pronunciation_map({word: replacement})

        with self._lock:
            data = self.load()
            existing = any(key.lower() == word.lower() for key in data)
            if not existing and len(data) >= MAX_LEXICON_ENTRIES:
                raise ValueError(
                    f"lexicon has {len(data)} entries, limit is {MAX_LEXICON_ENTRIES}; "
                    "delete an entry before adding another"
                )
            for key in list(data):
                if key.lower() == word.lower():
                    data.pop(key)
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


@lru_cache(maxsize=64)
def _compiled(
    mapping_key: tuple[tuple[str, str], ...],
) -> tuple[re.Pattern[str], dict[str, str]]:
    """Compile a mapping once; each entry becomes a named alternative.

    ``match.lastgroup`` identifies the replacement, so a Unicode case-fold match
    (``ſync`` for ``sync``, ``İstanbul`` for ``istanbul``) cannot raise the
    ``KeyError`` that a ``match.group(0).lower()`` lookup did.
    """
    ordered = sorted(mapping_key, key=lambda item: len(item[0]), reverse=True)
    groups: list[str] = []
    replacements: dict[str, str] = {}
    for index, (term, replacement) in enumerate(ordered):
        name = f"k{index}"
        groups.append(rf"(?P<{name}>(?<!\w){re.escape(term)}(?!\w))")
        replacements[name] = replacement
    return re.compile("|".join(groups), re.IGNORECASE), replacements


def apply_pronunciations(text: str, mapping: dict[str, str]) -> str:
    """Replace whole-word, case-insensitive keys in ``text``.

    Keys are tried longest-first so ``kubectl pod`` wins over ``kubectl``. The
    ``(?<!\\w)``/``(?!\\w)`` guards keep ``async`` out of ``asynchronously`` while
    still matching multi-word keys and keys containing punctuation.
    """
    if not text or not mapping:
        return text
    pattern, replacements = _compiled(tuple(sorted(mapping.items())))

    def replace(match: re.Match[str]) -> str:
        name = match.lastgroup
        return replacements[name] if name is not None else match.group(0)

    return pattern.sub(replace, text)


def prepare_text(
    store: PronunciationStore | None,
    text: str,
    overrides: dict[str, str] | None = None,
) -> str:
    """Return the spoken form of ``text`` after persistent + per-call replacement."""
    mapping = (
        store.effective(overrides) if store is not None else _clean_mapping(overrides)
    )
    return apply_pronunciations(text, mapping) if mapping else text
