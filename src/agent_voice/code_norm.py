"""Deterministic, regex-only rewriting of code-shaped text for speech.

``TTS`` reads ``GET /v1/voices`` as "gee eee tee slash vee one slash voices" at
best and skips it at worst; a narrated diff or a code walk-through is unlistenable
without help. This module turns the code-shaped *tokens* a caller asked for into
words an engine can say, and nothing else:

* ``worker_manager``        -> ``worker manager``
* ``useState``              -> ``use State``
* ``HTTPServer``            -> ``HTTP Server``
* ``iOS``                   -> ``i O S``
* ``v0.4.0``                -> ``version zero point four dot zero``
* ``3.12.1``                -> ``three point twelve point one``
* ``src/app.py``            -> ``src slash app dot py``
* ``->``                    -> ``to``
* ``=>``                    -> ``maps to``
* ``C++``                   -> ``C plus plus``

Three properties are load-bearing, and everything here exists to keep them:

**Protected spans.** :func:`spell_identifiers` never rewrites inside the spans it
is given. Those are the ranges an earlier lexicon pass produced
(:func:`agent_voice.pronunciations.apply_pronunciations_spans`), so
``kubectl -> "cube control"`` wins over any heuristic and a replacement can never
be expanded twice.

**Idempotent.** Every rule's output is a fixed point of the whole pipeline:
``worker manager``, ``HTTP Server``, ``i O S``, ``version zero point four dot
zero`` and ``src slash app dot py`` are all returned unchanged. Running the pass
twice therefore costs nothing and changes nothing.

**Bounded and deterministic.** No dictionaries of English words, no case
statistics, no randomness, no filesystem access. A pathological input cannot
inflate the output past :data:`MAX_EXPANSION_FACTOR` times its length (up to a
small floor, capped absolutely at :data:`MAX_OUTPUT_CHARS`); past the budget the
remainder is copied verbatim.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "MAX_CODE_CANDIDATES",
    "MAX_EXPANSION_FACTOR",
    "code_candidates",
    "spell_identifiers",
    "spell_out",
]

Span = tuple[int, int]
"""A half-open ``(start, end)`` range in the string a caller passes in."""

MAX_EXPANSION_FACTOR = 8
"""Ceiling on how much longer a rewrite may make its input."""

MIN_OUTPUT_BUDGET = 4096
"""The floor under the expansion budget, so a short input still gets expanded."""

MAX_OUTPUT_CHARS = 200_000
"""Absolute ceiling, independent of the input length."""

MAX_CODE_CANDIDATES = 200
"""How many suggestions :func:`code_candidates` will report."""

_MAX_SPELLED_ACRONYM = 3
"""An all-caps fragment at most this long is spelled letter by letter.

``iOS`` splits into ``i`` + ``OS``, and ``OS`` is read "O S"; ``HTTPServer``
splits into ``HTTP`` + ``Server``, and ``HTTP`` is read as one word.
"""

# ---------------------------------------------------------------------------
# Digit and number words
# ---------------------------------------------------------------------------

_DIGIT_WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
)
_TEEN_WORDS = (
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS_WORDS = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)


def _cardinal(value: int) -> str:
    """0-999 as words. Hyphens are avoided: engines phrase them inconsistently."""
    if value < 10:
        return _DIGIT_WORDS[value]
    if value < 20:
        return _TEEN_WORDS[value - 10]
    if value < 100:
        tens, ones = divmod(value, 10)
        return _TENS_WORDS[tens] if not ones else f"{_TENS_WORDS[tens]} {_DIGIT_WORDS[ones]}"
    hundreds, rest = divmod(value, 100)
    head = f"{_DIGIT_WORDS[hundreds]} hundred"
    return head if not rest else f"{head} {_cardinal(rest)}"


def _number_word(digits: str) -> str:
    """One version component as words.

    A canonical 1-3 digit component is read as a number (``12`` -> ``twelve``);
    a leading zero or a long component is read digit by digit (``007`` -> ``zero
    zero seven``, ``20240101`` -> eight digits), which is what the glyphs mean in
    a version, a build id or a date-ish string.
    """
    if not (digits.isascii() and digits.isdigit()):
        return digits
    if len(digits) <= 3 and (digits == "0" or not digits.startswith("0")):
        return _cardinal(int(digits))
    return " ".join(_DIGIT_WORDS[int(character)] for character in digits)


# ---------------------------------------------------------------------------
# Symbol words
# ---------------------------------------------------------------------------

_SYMBOL_WORDS = {
    "/": "slash",
    "\\": "backslash",
    ".": "dot",
    "-": "dash",
    "_": "underscore",
    "+": "plus",
    "=": "equals",
    ":": "colon",
    ";": "semicolon",
    ",": "comma",
    "?": "question mark",
    "!": "exclamation mark",
    "@": "at",
    "#": "hash",
    "$": "dollar",
    "%": "percent",
    "&": "and",
    "*": "asterisk",
    "(": "open paren",
    ")": "close paren",
    "[": "open bracket",
    "]": "close bracket",
    "{": "open brace",
    "}": "close brace",
    "<": "less than",
    ">": "greater than",
    "|": "pipe",
    "~": "tilde",
    "^": "caret",
    "`": "backtick",
    '"': "quote",
    "'": "apostrophe",
}

# ---------------------------------------------------------------------------
# Identifier, version and path rules
# ---------------------------------------------------------------------------

_ARROW = re.compile(r"\s*->\s*")
_MAPS_TO = re.compile(r"\s*=>\s*")
_PLUS_PLUS = re.compile(r"(?<!\w)([A-Za-z]{1,3})\+\+")

_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*")
"""A word-ish run: letters, digits and interior underscores. ``arr[0]`` never
matches as one token, which is why indexing survives untouched."""

_CASE_BOUNDARY = re.compile(r"[a-z0-9][A-Z]|[A-Z][A-Z][a-z]")
"""A camel hump (``useState``) or an acronym tail (``HTTPServer``)."""

_CASE_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
"""Split points inside one underscore-free part."""

_VERSION_V = re.compile(r"(?<![\w.])([vV])([0-9]+(?:\.[0-9]+)+)(?!\w|\.\d)")
"""A ``v``-prefixed version. One dot is enough: ``v1.2`` is a version."""

_VERSION_BARE = re.compile(r"(?<![\w.])([0-9]+(?:\.[0-9]+){2,})(?!\w|\.\d)")
"""An unprefixed version needs two dots, so ``3.5 seconds`` stays a number."""

_TOKEN = re.compile(r"\S+")
"""One whitespace-free run: the unit the path/filename rule inspects."""

_FILENAME_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9_]{1,9}$")
"""``app.py``, ``page.html``. Two characters minimum, so ``e.g.`` is prose.

A one-letter extension (``graph.h``) is left alone on purpose: single-letter
dotted suffixes are overwhelmingly sentence punctuation or abbreviations, and a
missed ``.h`` costs a listener less than every ``i.e.`` becoming "i dot e".
"""

_TRAILING_PUNCTUATION = re.compile(r"[.,;:!?]+$")
"""Sentence punctuation peeled off a path token before it is expanded."""

_MULTI_SPACE = re.compile(r" {2,}")


def _split_case(part: str) -> list[str]:
    """Split one underscore-free part into spoken words."""
    words: list[str] = []
    for piece in _CASE_SPLIT.split(part):
        if not piece:
            continue
        if 1 < len(piece) <= _MAX_SPELLED_ACRONYM and piece.isupper():
            words.extend(piece)
        else:
            words.append(piece)
    return words


def _expand_token(match: re.Match[str]) -> str:
    token = match.group(0)
    if "_" not in token and _CASE_BOUNDARY.search(token) is None:
        return token
    words: list[str] = []
    for part in token.split("_"):
        if part:
            words.extend(_split_case(part))
    return " ".join(words) if words else token


def _expand_version_v(match: re.Match[str]) -> str:
    words = [_number_word(part) for part in match.group(2).split(".")]
    spoken = words[0]
    for index, word in enumerate(words[1:], start=1):
        # "v0.4.0" is read "version zero point four dot zero": the first
        # separator is the decimal point, the rest are literal dots.
        spoken += (" point " if index == 1 else " dot ") + word
    return f"version {spoken}"


def _expand_version_bare(match: re.Match[str]) -> str:
    return " point ".join(_number_word(part) for part in match.group(1).split("."))


def _expand_plus_plus(match: re.Match[str]) -> str:
    return f"{match.group(1)} plus plus"


def _expand_separators(token: str) -> str:
    """Speak the separators and dots inside one path/filename token."""
    token = token.replace("\\", " backslash ")
    token = token.replace("/", " slash ")
    token = token.replace(".", " dot ")
    return _MULTI_SPACE.sub(" ", token).strip()


def _expand_path_token(match: re.Match[str]) -> str:
    """Expand ``src/app.py`` and bare ``app.py``; everything else is untouched."""
    token = match.group(0)
    body = token
    suffix = ""
    punctuation = _TRAILING_PUNCTUATION.search(body)
    if punctuation is not None:
        body = body[: punctuation.start()]
        suffix = token[len(body) :]
    if "/" not in body and "\\" not in body:
        if _FILENAME_EXTENSION.search(body) is None:
            return token
    expanded = _expand_separators(body)
    return f"{expanded}{suffix}" if expanded else token


def _expand_identifiers(chunk: str) -> str:
    """Apply the whole rule set to one unprotected chunk.

    Order matters and is fixed: operators, then ``++``, then identifiers, then
    versions, then paths. Identifiers come before paths so ``src/appHelper.py``
    becomes ``src slash app Helper dot py`` rather than keeping an unreadable
    hump, and versions come before paths so ``app/v0.4.0/x.md`` keeps its version
    reading instead of turning every dot into "dot".
    """
    if not chunk:
        return chunk
    text = _ARROW.sub(" to ", chunk)
    text = _MAPS_TO.sub(" maps to ", text)
    text = _PLUS_PLUS.sub(_expand_plus_plus, text)
    text = _IDENTIFIER.sub(_expand_token, text)
    text = _VERSION_V.sub(_expand_version_v, text)
    text = _VERSION_BARE.sub(_expand_version_bare, text)
    return _TOKEN.sub(_expand_path_token, text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _clamp_spans(spans: Iterable[Span] | None, length: int) -> list[Span]:
    """Clamp, drop empties, sort and merge — the spans a caller actually meant."""
    if not spans:
        return []
    cleaned: list[Span] = []
    for span in spans:
        try:
            start, end = span
            start, end = int(start), int(end)
        except (TypeError, ValueError):
            continue
        start = max(0, min(start, length))
        end = max(0, min(end, length))
        if end > start:
            cleaned.append((start, end))
    cleaned.sort()
    merged: list[Span] = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def spell_identifiers(text: str, protected: Sequence[Span] | None = None) -> str:
    """Expand code-shaped tokens in ``text``, leaving ``protected`` untouched.

    ``protected`` holds half-open ``(start, end)`` ranges *in ``text``* — the
    spans returned by ``apply_pronunciations_spans`` or the inner text of a
    ``[spell]`` segment. Only the complement is rewritten; the protected
    characters are copied through byte for byte, so a lexicon replacement (which
    may itself look like code) is never re-expanded.

    Idempotent: an already-spelled text is returned unchanged. Bounded: past
    ``MAX_EXPANSION_FACTOR`` times the input length the remainder is copied
    verbatim rather than expanded.
    """
    if not text:
        return ""
    spans = _clamp_spans(protected, len(text))
    budget = min(
        max(len(text) * MAX_EXPANSION_FACTOR, MIN_OUTPUT_BUDGET), MAX_OUTPUT_CHARS
    )
    pieces: list[str] = []
    produced = 0
    cursor = 0
    for start, end in (*spans, (len(text), len(text))):
        if cursor < start:
            chunk = _expand_identifiers(text[cursor:start])
            if produced + len(chunk) > budget:
                pieces.append(text[cursor:])
                return "".join(pieces)
            pieces.append(chunk)
            produced += len(chunk)
        if start < end:
            pieces.append(text[start:end])
            produced += end - start
        cursor = end
    return "".join(pieces)


def spell_out(text: str) -> str:
    """Spell every glyph of a ``[spell]`` segment: ``TCP/IP`` -> ``T C P slash I P``.

    Letters stay letters (upper-cased, because engines read capitals more
    reliably as letters), digits become words, and punctuation becomes its spoken
    name. Whitespace only separates: ``"TCP / IP"`` and ``"TCP/IP"`` spell the
    same way. An unknown symbol is passed through, so nothing is silently lost.
    """
    if not text:
        return ""
    tokens: list[str] = []
    for character in text:
        if character.isspace():
            continue
        if character.isascii() and character.isalpha():
            tokens.append(character.upper())
        elif character.isascii() and character.isdigit():
            tokens.append(_DIGIT_WORDS[int(character)])
        else:
            tokens.append(_SYMBOL_WORDS.get(character, character))
    return " ".join(tokens)


_ACRONYM = re.compile(r"(?<![\w.])([A-Z][A-Z0-9]{1,7})(?!\w)")
_PLUS_PLUS_TERM = re.compile(r"(?<!\w)([A-Za-z]{1,3}\+\+)")


def _known_terms(lexicon: Mapping[str, str] | Iterable[str] | None) -> frozenset[str]:
    """Lexicon keys in the form :func:`code_candidates` compares against."""
    if lexicon is None:
        return frozenset()
    if isinstance(lexicon, str):
        terms: Iterable[str] = (lexicon,)
    elif isinstance(lexicon, Mapping):
        terms = lexicon.keys()
    else:
        terms = lexicon
    known: set[str] = set()
    for term in terms:
        key = unicodedata.normalize("NFC", str(term)).lower()
        if key:
            known.add(key)
    return frozenset(known)


def code_candidates(
    text: str, lexicon: Mapping[str, str] | Iterable[str] | None = None
) -> list[str]:
    """Code-shaped terms in ``text`` that ``lexicon`` does not cover.

    The input of ``agent-voice pronunciations check``: it answers "what here will
    the engine mispronounce, and has nobody taught it yet?" with the *whole-word*
    candidates an operator can paste into ``pronunciations set``. All-caps
    acronyms (``CUDA``, ``HTTP``), snake_case and camelCase identifiers
    (``worker_manager``, ``HTTPServer``) and ``C++``-style terms are reported.

    Deterministic: sorted, de-duplicated case-insensitively, first spelling wins,
    at most :data:`MAX_CODE_CANDIDATES` results. A ``lexicon`` may be the store's
    map (its keys are what count) or any iterable of terms; comparison is on the
    NFC-normalised, lower-cased form the lexicon uses.
    """
    if not text:
        return []
    known = _known_terms(lexicon)
    found: dict[str, str] = {}

    def remember(term: str) -> None:
        key = unicodedata.normalize("NFC", term).lower()
        if not key or key in known or key in found:
            return
        found[key] = term

    for match in _ACRONYM.finditer(text):
        remember(match.group(1))
    for match in _IDENTIFIER.finditer(text):
        token = match.group(0)
        if "_" in token or _CASE_BOUNDARY.search(token):
            remember(token)
    for match in _PLUS_PLUS_TERM.finditer(text):
        remember(match.group(1))
    return sorted(found.values())[:MAX_CODE_CANDIDATES]
