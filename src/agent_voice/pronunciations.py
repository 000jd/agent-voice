"""Persistent pronunciation lexicons and spoken-text rewriting.

A TTS engine reads what it is given: ``kubectl`` becomes something no listener
recognises. The lexicon is a small, operator-owned replacement map persisted at
``data/pronunciations.json``; per-call overrides sit on top of it. Replacement is
case-insensitive and whole-word, so ``async`` never corrupts ``asynchronously``.

v0.4.0 adds *scopes*. :class:`ScopedLexicon` layers the global map with a project
map (``data/pronunciations/<project>.json``), a voice map
(``data/voices/<voice_id>/pronunciations.json``) and finally the caller's own
overrides. Later layers win; keys are lower-cased and NFC-normalised so
``Kubectl`` and ``kubectl`` are one entry. Merge maps first and compile once —
compiled patterns are never concatenated, and every scope keeps its own cap.

The rewritten spoken text is what the synthesis pipeline speaks and what the
synthesis cache keys on: adding an unrelated entry leaves existing renders cached,
while changing a term that appears in a line changes that line's key. Subtitles
keep the original written text.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import unicodedata
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from typing import Any

from filelock import FileLock

PRONUNCIATION_FILE = "pronunciations.json"
PROJECT_LEXICON_DIR = "pronunciations"
"""``<data>/pronunciations/`` holds one ``<project>.json`` per scoped lexicon."""

MAX_LEXICON_ENTRIES = 500
MAX_MERGED_LEXICON_ENTRIES = 1500
MAX_LEXICON_TERM_CHARS = 200
MAX_SCOPED_MERGE_CACHE = 64
"""How many (project, voice) merges an instance may memoise."""

TRIM_SUGGESTION = (
    f"Keep at most {MAX_LEXICON_ENTRIES} entries per scope "
    f"({MAX_MERGED_LEXICON_ENTRIES} merged) with {MAX_LEXICON_TERM_CHARS} characters per key "
    "and per replacement: delete unused terms or shorten the longest ones, then retry."
)
"""How to fix an over-cap map. Shared by REST, MCP and the WebSocket surface."""

logger = logging.getLogger(__name__)


def normalize_lexicon_key(value: str) -> str:
    """The matching form of a lexicon key: NFC-normalised, then case-folded.

    Normalising *after* the case fold matters: ``"İ".lower()`` is two code points
    (``i`` + combining dot above), so a single pre-fold NFC pass would leave the
    key decomposed and two spellings of one term would never collide.
    """
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", value).lower())


def normalize_lexicon_value(value: str) -> str:
    """Spoken replacements are NFC-normalised but never case-folded."""
    return unicodedata.normalize("NFC", value)


def validate_lexicon_map(
    mapping: dict[str, str] | None, *, max_entries: int = MAX_LEXICON_ENTRIES
) -> None:
    """Reject an oversized lexicon before it reaches the regex or a worker."""
    if not mapping:
        return
    if len(mapping) > max_entries:
        raise ValueError(
            f"pronunciation map has {len(mapping)} entries, limit is {max_entries}"
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


def validate_pronunciation_map(mapping: dict[str, str] | None) -> None:
    """The single-scope gate: at most :data:`MAX_LEXICON_ENTRIES` entries.

    Kept as the module's historical entry point (``schemas`` uses it as a pydantic
    field validator). A merged multi-scope map is validated with the larger
    :data:`MAX_MERGED_LEXICON_ENTRIES` cap instead.
    """
    validate_lexicon_map(mapping, max_entries=MAX_LEXICON_ENTRIES)


def check_lexicon_map(
    mapping: dict[str, str] | None,
    *,
    context: str = "invalid pronunciations",
    max_entries: int = MAX_LEXICON_ENTRIES,
) -> None:
    """Validate a caller-supplied map, raising the structured ``BAD_REQUEST``.

    ``validate_lexicon_map`` stays a plain ``ValueError`` because pydantic field
    validators want one; every surface that answers a client — REST, MCP and the
    WebSocket — goes through this wrapper instead, so an over-cap map becomes the
    canonical ``{error_code, message, suggestion}`` body with a way out.
    """
    try:
        validate_lexicon_map(mapping, max_entries=max_entries)
    except ValueError as exc:
        # Deferred: schemas imports this module for its field validators, so a
        # module-level import here would be circular.
        from .schemas import AgentVoiceError, ErrorCode

        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST, f"{context}: {exc}", TRIM_SUGGESTION
        ) from exc


def check_pronunciation_map(
    mapping: dict[str, str] | None, *, context: str = "invalid pronunciations"
) -> None:
    """Validate a single-scope map, raising the structured ``BAD_REQUEST``."""
    check_lexicon_map(mapping, context=context, max_entries=MAX_LEXICON_ENTRIES)


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

    def _read(self) -> tuple[dict[str, str], bool]:
        """The cleaned on-disk map, plus whether it survived the caps.

        The file is operator-owned and hand-editable, so an over-cap or malformed
        entry list must never compile an unbounded regex: it is dropped wholesale
        (with a warning) rather than crashing the daemon or a single speak() call.
        The boolean lets a writer refuse to clobber a file it could not read
        instead of silently replacing 600 entries with one.
        """
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}, True
        cleaned = _clean_mapping(raw)
        try:
            validate_pronunciation_map(cleaned)
        except ValueError as exc:
            logger.warning(
                "ignoring %s until it is trimmed (%s); no pronunciations apply",
                self.path,
                exc,
            )
            return {}, False
        return cleaned, True

    def load(self) -> dict[str, str]:
        return self._read()[0]

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
            data, readable = self._read()
            if not readable:
                # Rewriting here would replace the operator's over-cap file with
                # this one entry, silently destroying the rest of the lexicon.
                raise ValueError(
                    f"{self.path} is over the lexicon caps; trim it before "
                    "adding or removing entries"
                )
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
            data, readable = self._read()
            if not readable:
                # Same reason as set(): one delete must not rewrite a file the
                # caps rejected wholesale.
                raise ValueError(
                    f"{self.path} is over the lexicon caps; trim it before "
                    "adding or removing entries"
                )
            matches = [key for key in data if key.lower() == needle]
            if not matches:
                return False
            for key in matches:
                data.pop(key)
            self._write(data)
        return True

    def effective(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
        """Persistent map plus per-call overrides, lower-cased for matching.

        The merged map is what actually reaches the regex, so it is the map that
        must satisfy the caps: a valid 500-entry file plus a valid 500-entry
        per-call map would otherwise compile a 1,000-alternative pattern. An
        over-cap merge is the caller's ``BAD_REQUEST`` with a trim suggestion,
        never an ``INTERNAL_ERROR`` and never a slower request.
        """
        merged = {key.lower(): replacement for key, replacement in self.load().items()}
        for key, replacement in _clean_mapping(overrides).items():
            merged[key.lower()] = replacement
        check_pronunciation_map(
            merged, context="pronunciation lexicon plus overrides exceed the caps"
        )
        return merged


# A voice id is a directory name: no separators, no traversal, no surprises.
_VOICE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

ScopeSignature = tuple[tuple[int, int], ...]
"""Per-file ``(mtime_ns, size)`` stamps that identify a merge's inputs."""

MergedScope = tuple[ScopeSignature, dict[str, str]]
"""A cached merge: the signature it was built from, plus the map itself."""


def _voice_file_names() -> tuple[str, str]:
    """``(metadata, lexicon)`` filenames, owned by :mod:`agent_voice.voice_store`.

    Imported lazily: ``voice_store`` imports ``schemas``, and ``schemas`` imports
    this module for its field validators, so a module-level import would be
    circular.
    """
    from .voice_store import LEXICON_NAME, METADATA_NAME

    return METADATA_NAME, LEXICON_NAME


class ScopedLexicon:
    """Layered pronunciation lexicons: global, project, voice, then per-call.

    Precedence is fixed and later layers win::

        <data>/pronunciations.json                        global
        <data>/pronunciations/<project>.json              project
        <data>/voices/<voice_id>/pronunciations.json      voice
        the ``overrides`` argument                        per call

    Every scope is read through a :class:`PronunciationStore`, so a hand-edited
    over-cap file is ignored wholesale with a warning instead of compiling a
    pattern nobody bounded. The merged result must satisfy
    :data:`MAX_MERGED_LEXICON_ENTRIES`; that is the only place the larger cap
    applies.

    Merges are memoised on ``(project, voice_id)`` and validated against the
    participating files' ``(mtime_ns, size)`` stamps, so an edit — through
    :meth:`store_for`, the CLI, or an operator's editor — is picked up on the next
    call without re-reading the files, and one scope's edit never invalidates
    another scope's entry. The cache is not lock-protected: every operation on it
    is a single atomic dict operation, so the worst a concurrent request can do is
    recompute a merge that another thread is also computing.
    """

    def __init__(
        self,
        data_dir: Path,
        voices_dir: Path,
        *,
        global_path: Path | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.voices_dir = Path(voices_dir)
        self.global_path = (
            Path(global_path)
            if global_path is not None
            else self.data_dir / PRONUNCIATION_FILE
        )
        self.projects_dir = self.data_dir / PROJECT_LEXICON_DIR
        self._stores: dict[Path, PronunciationStore] = {}
        self._merges: OrderedDict[tuple[str | None, str | None], MergedScope] = (
            OrderedDict()
        )

    # -- paths ----------------------------------------------------------
    def path_for_project(self, project: str) -> Path:
        """``<data>/pronunciations/<project>.json`` for a validated slug.

        Raises ``ValueError`` (from ``schemas.validate_slug``) for anything that
        is not a lowercase slug: the project name becomes a filename, so it is
        validated, never interpolated.
        """
        # Deferred: schemas imports this module for its field validators.
        from .schemas import validate_slug

        slug = validate_slug(project)
        return self.projects_dir / f"{slug}.json"

    def path_for_voice(self, voice_id: str | None) -> Path | None:
        """``<voices>/<voice_id>/pronunciations.json``, or ``None``.

        ``None`` means "this scope does not exist": no voice id, an id that is not
        a plain directory name, or a directory without ``metadata.json`` — the
        same existence rule :meth:`VoiceStore.resolve` uses, so a preset speaker
        (``af_heart``) or a stale id simply has no voice lexicon. The path is
        built by joining a validated component, never by interpolating the request
        string into a pattern or a format.
        """
        if not isinstance(voice_id, str) or not voice_id:
            return None
        if _VOICE_ID_PATTERN.fullmatch(voice_id) is None or ".." in voice_id:
            return None
        metadata_name, lexicon_name = _voice_file_names()
        voice_dir = self.voices_dir / voice_id
        try:
            if voice_dir.resolve().parent != self.voices_dir.resolve():
                return None
        except OSError:  # pragma: no cover - resolve() is best-effort
            return None
        if not (voice_dir / metadata_name).is_file():
            return None
        return voice_dir / lexicon_name

    def scope_paths(
        self, project: str | None = None, voice_id: str | None = None
    ) -> list[Path]:
        """The files that feed one merge, in precedence order.

        Missing files are included: this is a diagnostic/CLI view of *where* a
        scope lives, not a list of what exists. Raises ``ValueError`` for an
        invalid project slug.
        """
        paths = [self.global_path]
        if project is not None:
            paths.append(self.path_for_project(project))
        voice_path = self.path_for_voice(voice_id)
        if voice_path is not None:
            paths.append(voice_path)
        return paths

    def _project_path(self, project: str) -> Path:
        """A validated project path, raising the structured ``BAD_REQUEST``."""
        try:
            return self.path_for_project(project)
        except ValueError as exc:
            from .schemas import AgentVoiceError, ErrorCode

            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"invalid pronunciation project: {exc}",
                "Project names are lowercase slugs: letters, digits and underscores, "
                "2-41 characters, not starting with an underscore.",
            ) from exc

    # -- stores ---------------------------------------------------------
    def store_for(
        self, project: str | None = None, voice_id: str | None = None
    ) -> PronunciationStore:
        """The store a CLI write targets: one scope, never a merge.

        ``ValueError`` for an unknown voice or an invalid slug, and for passing
        both scopes at once: silently writing one of them would look like success
        while the other lexicon stayed untouched.
        """
        if project is not None and voice_id is not None:
            raise ValueError(
                "a pronunciation write targets one scope: pass project= or "
                "voice_id=, not both"
            )
        if voice_id is not None:
            path = self.path_for_voice(voice_id)
            if path is None:
                raise ValueError(
                    f"no such voice: {voice_id} (a voice lexicon lives next to the "
                    "voice's metadata.json)"
                )
            return self._store(path)
        if project is not None:
            return self._store(self.path_for_project(project))
        return self._store(self.global_path)

    def _store(self, path: Path) -> PronunciationStore:
        store = self._stores.get(path)
        if store is None:
            store = PronunciationStore(path)
            self._stores[path] = store
        return store

    # -- merge ----------------------------------------------------------
    def effective(
        self,
        project: str | None = None,
        voice_id: str | None = None,
        overrides: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """The merged map that actually reaches the regex, for one request.

        Keys are lower-cased and NFC-normalised; replacements are NFC-normalised.
        The returned dict is a fresh copy, so a caller may annotate it freely.
        """
        merged = dict(self._merged_scope_map(project, voice_id))
        per_call = _clean_mapping(overrides)
        if per_call:
            # Per-call overrides are a scope too: they obey the per-scope cap on
            # their own, and the merged result obeys the merged cap below.
            check_lexicon_map(per_call, context="pronunciation overrides exceed the caps")
            for key in sorted(per_call):
                merged[normalize_lexicon_key(key)] = normalize_lexicon_value(
                    per_call[key]
                )
        check_lexicon_map(
            merged,
            max_entries=MAX_MERGED_LEXICON_ENTRIES,
            context="pronunciation lexicon scopes plus overrides exceed the caps",
        )
        return merged

    def _merged_scope_map(
        self, project: str | None, voice_id: str | None
    ) -> dict[str, str]:
        """Global -> project -> voice, memoised on the inputs' file stamps."""
        paths = [self.global_path]
        if project is not None:
            paths.append(self._project_path(project))
        voice_path = self.path_for_voice(voice_id)
        if voice_path is not None:
            paths.append(voice_path)

        signature: ScopeSignature = tuple(self._signature(path) for path in paths)
        # A voice without a lexicon file contributes nothing, so it shares the
        # key (and the cached merge) of "no voice".
        key = (project, voice_id if voice_path is not None else None)
        entry = self._merges.get(key)
        if entry is not None and entry[0] == signature:
            self._merges.move_to_end(key)  # least-recently-used goes first
            return entry[1]

        merged: dict[str, str] = {}
        for path in paths:
            merged.update(self._scope_map(path))
        self._merges[key] = (signature, merged)
        while len(self._merges) > MAX_SCOPED_MERGE_CACHE:
            self._merges.popitem(last=False)
        return merged

    def _scope_map(self, path: Path) -> dict[str, str]:
        """One scope's normalised map. An over-cap file reads as empty.

        ``PronunciationStore._read`` is the cap gate: a 600-entry file logs a
        warning and contributes nothing, which is the same recoverable answer the
        global lexicon has always given.
        """
        raw = self._store(path).load()
        return {
            normalize_lexicon_key(key): normalize_lexicon_value(replacement)
            for key, replacement in sorted(raw.items())
        }

    @staticmethod
    def _signature(path: Path) -> tuple[int, int]:
        """``(mtime_ns, size)`` — the edit detector behind the merge cache.

        ``os.replace`` gives an atomic write a fresh inode and a fresh mtime, so
        an operator edit and a :meth:`store_for` write both change this. A missing
        file has a stable sentinel signature, which is what lets "no project
        lexicon yet" cache as cheaply as an existing one.
        """
        try:
            stat = path.stat()
        except OSError:
            return (0, -1)
        return (stat.st_mtime_ns, stat.st_size)

    # -- observability --------------------------------------------------
    def hash(
        self,
        project: str | None = None,
        voice_id: str | None = None,
        overrides: dict[str, str] | None = None,
    ) -> str:
        """SHA-256 of the canonical JSON of :meth:`effective`.

        Observability only. It is never a synthesis cache key: the rewritten
        spoken text is already what the cache keys on, and ``project`` is
        deliberately not a key field.
        """
        return lexicon_hash(
            self.effective(project=project, voice_id=voice_id, overrides=overrides)
        )

    def clear_cache(self) -> None:
        """Drop the merge cache. The file stamps make this belt-and-braces only."""
        self._merges.clear()


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


def apply_pronunciations_spans(
    text: str,
    mapping: dict[str, str],
    *,
    max_entries: int = MAX_MERGED_LEXICON_ENTRIES,
) -> tuple[str, list[tuple[int, int]]]:
    """Replace whole-word, case-insensitive keys and report where they landed.

    Returns ``(rewritten, spans)``. Each span is a half-open ``(start, end)``
    range **in the returned text** covering exactly the characters one replacement
    produced; the complement of the spans is input that this function did not
    change. Downstream rewriting (``code_norm``'s identifier expansion) must skip
    the spans — that is what makes "a lexicon entry wins over the heuristic, and a
    replacement is never re-processed" structural rather than a convention. A
    replacement that expands to nothing yields an empty (zero-width) span.

    Keys are tried longest-first so ``kubectl pod`` wins over ``kubectl``. The
    ``(?<!\\w)``/``(?!\\w)`` guards keep ``async`` out of ``asynchronously`` while
    still matching multi-word keys and keys containing punctuation.

    The caps are re-checked here because this is the last gate before
    ``_compiled``: any future caller that hands over a raw map — a WebSocket
    payload, an OpenAI-shaped body, a hand-edited file — still cannot compile a
    pattern larger than the limits allow. ``max_entries`` defaults to the merged
    cap because a scoped request hands over a merged map; the single-scope entry
    point below passes the per-scope cap.
    """
    if not text or not mapping:
        return text, []
    check_lexicon_map(mapping, max_entries=max_entries)
    pattern, replacements = _compiled(tuple(sorted(mapping.items())))

    pieces: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    produced = 0
    for match in pattern.finditer(text):
        name = match.lastgroup
        if name is None:  # pragma: no cover - every alternative is a named group
            continue
        replacement = replacements[name]
        plain = text[cursor : match.start()]
        pieces.append(plain)
        produced += len(plain)
        start = produced
        pieces.append(replacement)
        produced += len(replacement)
        spans.append((start, produced))
        cursor = match.end()
    if not spans:
        return text, []
    pieces.append(text[cursor:])
    return "".join(pieces), spans


def apply_pronunciations(
    text: str,
    mapping: dict[str, str],
    *,
    max_entries: int = MAX_LEXICON_ENTRIES,
) -> str:
    """Replace whole-word, case-insensitive keys in ``text``.

    The single-scope convenience wrapper: it keeps the historical 500-entry gate,
    so a map that never went through a scope merge is rejected before it can
    compile. A merged multi-scope map belongs in
    :func:`apply_pronunciations_spans`, which allows the merged cap and also
    returns the protected spans.
    """
    return apply_pronunciations_spans(text, mapping, max_entries=max_entries)[0]


def lexicon_hash(mapping: dict[str, str]) -> str:
    """SHA-256 over canonical JSON (sorted keys, no insignificant whitespace)."""
    canonical = json.dumps(
        dict(mapping), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_mapping(
    store: PronunciationStore | None,
    overrides: dict[str, str] | None = None,
    *,
    lexicon: ScopedLexicon | None = None,
    project: str | None = None,
    voice_id: str | None = None,
) -> dict[str, str]:
    """The map one request should be spoken through.

    ``lexicon`` wins when given: its global scope *is* ``store``'s file (both come
    from the same settings), so honouring the store as well would merge it twice.
    """
    if lexicon is not None:
        return lexicon.effective(
            project=project, voice_id=voice_id, overrides=overrides
        )
    if store is not None:
        return store.effective(overrides)
    return _clean_mapping(overrides)


def prepare_text(
    store: PronunciationStore | None,
    text: str,
    overrides: dict[str, str] | None = None,
    *,
    lexicon: ScopedLexicon | None = None,
    project: str | None = None,
    voice_id: str | None = None,
) -> str:
    """Return the spoken form of ``text`` after every applicable lexicon layer.

    Back-compatible: the first three positional parameters behave exactly as they
    always have. Pass ``lexicon`` (plus ``project``/``voice_id``) to include the
    scoped layers; the merged cap applies instead of the per-scope one.

    Both halves validate before anything compiles: ``effective()`` checks the
    persistent + per-call merge, and ``apply_pronunciations_spans`` re-checks the
    map it is handed, so no route can reach ``_compiled`` with an over-cap map.
    """
    return prepare_text_spans(
        store,
        text,
        overrides,
        lexicon=lexicon,
        project=project,
        voice_id=voice_id,
    )[0]


def prepare_text_spans(
    store: PronunciationStore | None,
    text: str,
    overrides: dict[str, str] | None = None,
    *,
    lexicon: ScopedLexicon | None = None,
    project: str | None = None,
    voice_id: str | None = None,
) -> tuple[str, list[tuple[int, int]]]:
    """:func:`prepare_text`, plus the protected spans of the rewritten text."""
    mapping = resolve_mapping(
        store, overrides, lexicon=lexicon, project=project, voice_id=voice_id
    )
    if not mapping:
        return text, []
    limit = MAX_MERGED_LEXICON_ENTRIES if lexicon is not None else MAX_LEXICON_ENTRIES
    return apply_pronunciations_spans(text, mapping, max_entries=limit)


__all__ = [
    "MAX_LEXICON_ENTRIES",
    "MAX_LEXICON_TERM_CHARS",
    "MAX_MERGED_LEXICON_ENTRIES",
    "MAX_SCOPED_MERGE_CACHE",
    "PROJECT_LEXICON_DIR",
    "PRONUNCIATION_FILE",
    "TRIM_SUGGESTION",
    "PronunciationStore",
    "ScopedLexicon",
    "apply_pronunciations",
    "apply_pronunciations_spans",
    "check_lexicon_map",
    "check_pronunciation_map",
    "lexicon_hash",
    "normalize_lexicon_key",
    "normalize_lexicon_value",
    "prepare_text",
    "prepare_text_spans",
    "resolve_mapping",
    "validate_lexicon_map",
    "validate_pronunciation_map",
]
