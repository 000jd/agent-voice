"""One place where the *spoken* form of a line is derived from its written form.

Three rewriting layers sit between what a caller types and what the engine says,
and their order is not negotiable:

1. **Scoped lexicon** — ``global -> project -> voice -> per-call`` word
   replacements (``kubectl`` -> ``cube control``). An operator wrote these on
   purpose, so they win over any heuristic.
2. **Code normalization** — opt-in expansion of identifiers, versions, paths and
   operators. It only ever sees the *complement* of the lexicon: the spans the
   lexicon just rewrote are protected, so ``kubectl`` can never be expanded a
   second time into "kube control" soup.
3. **Spelled blocks** — ``[spell]...[/spell]`` is literal characters ("v zero dot
   four dot zero"). It is protected from BOTH layers: it is already exactly what
   the engine must say.

Everything here is a thin, deliberately boring adapter: the primitives live in
:mod:`agent_voice.pronunciations` (scoped lexicon) and
:mod:`agent_voice.code_norm` (heuristics), and the daemon must keep working when
the optional pieces are absent. The late imports are what make that possible and
are the only reason this module is not three one-liners at the call sites — every
route, script beat, stream frame and MCP tool goes through the same functions, so
the three layers can never be applied in a different order in one place.
"""

from __future__ import annotations

import logging
from typing import Any

from .pronunciations import apply_pronunciations

logger = logging.getLogger(__name__)

_DIGITS = (
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

_SYMBOLS: dict[str, str] = {
    ".": "dot",
    "/": "slash",
    "\\": "backslash",
    "-": "dash",
    "_": "underscore",
    ":": "colon",
    ";": "semicolon",
    ",": "comma",
    "=": "equals",
    "+": "plus",
    "&": "and",
    "*": "star",
    "#": "hash",
    "@": "at",
    "$": "dollar",
    "%": "percent",
    "^": "caret",
    "~": "tilde",
    "|": "pipe",
    "<": "less than",
    ">": "greater than",
    "(": "open paren",
    ")": "close paren",
    "[": "open bracket",
    "]": "close bracket",
    "{": "open brace",
    "}": "close brace",
    "'": "apostrophe",
    '"': "quote",
    "`": "backtick",
    "?": "question mark",
    "!": "exclamation mark",
}
"""Character names for the fallback speller; ``code_norm.spell_out`` is
authoritative whenever it is installed."""


def _code_norm() -> Any | None:
    """The optional code-normalization module, or ``None`` when unavailable."""
    try:
        from . import code_norm
    except ImportError:  # pragma: no cover - only while code_norm is absent
        return None
    return code_norm


def _fallback_spell_out(text: str) -> str:
    """Spell characters one at a time: ``TCP/IP`` -> ``T C P slash I P``.

    A bridge so ``[spell]`` behaves even when :mod:`agent_voice.code_norm` is not
    importable. It deliberately implements the plainest possible reading (one
    token per character, digits as words) rather than trying to match the
    heuristics in ``code_norm``.
    """
    tokens: list[str] = []
    for char in text:
        if char.isspace():
            continue
        if char.isdigit():
            tokens.append(_DIGITS[int(char)])
        elif char.isalpha():
            tokens.append(char)
        else:
            tokens.append(_SYMBOLS.get(char, char))
    return " ".join(tokens)


def spell_text(text: str) -> str:
    """Spell a ``[spell]`` block out loud, character by character."""
    if not text:
        return text
    module = _code_norm()
    if module is not None:
        return module.spell_out(text)
    return _fallback_spell_out(text)


def lexicon_mapping(
    ctx: Any,
    *,
    project: str | None,
    voice_id: str | None,
    overrides: dict[str, str] | None,
) -> dict[str, str]:
    """The effective lexicon for one request: global -> project -> voice -> call.

    ``voice_id`` is the *resolved* one, which is why this runs after
    ``resolve_target``: an auto-selected voice brings its own vocabulary.
    """
    lexicon = getattr(ctx, "lexicon", None)
    if lexicon is not None:
        return lexicon.effective(project, voice_id, overrides)
    # Fallback for a context without a ScopedLexicon (unit tests, or a build
    # without the scoped layers): the global map plus per-call overrides.
    return ctx.pronunciations.effective(overrides)


def apply_lexicon(text: str, mapping: dict[str, str]) -> tuple[str, list[tuple[int, int]]]:
    """Rewrite ``text`` with ``mapping`` and report the spans that changed.

    The spans are offsets into the *returned* text, which is what makes the next
    step safe: code normalization is told to leave those characters alone.
    """
    if not text or not mapping:
        return text, []
    from . import pronunciations as pronunciation_module

    spans_fn = getattr(pronunciation_module, "apply_pronunciations_spans", None)
    if spans_fn is not None:
        return spans_fn(text, mapping)
    return apply_pronunciations(text, mapping), []


def expand_code_identifiers(
    text: str, *, protected: list[tuple[int, int]] | tuple[tuple[int, int], ...] = ()
) -> str:
    """Expand code-like tokens outside ``protected`` spans, when available."""
    if not text:
        return text
    module = _code_norm()
    if module is None:
        logger.debug("code_norm is unavailable; leaving identifiers as written")
        return text
    return module.spell_identifiers(text, list(protected))


def normalize_text(
    text: str,
    *,
    mapping: dict[str, str] | None = None,
    spell: bool = False,
    expand_identifiers: bool = False,
) -> str:
    """The spoken form of one speech run.

    Args:
        text: The run's written text, markup already stripped.
        mapping: The effective scoped lexicon; applied first.
        spell: True inside ``[spell]`` — characters are spelled and no other
            layer touches the run.
        expand_identifiers: Apply ``code_norm.spell_identifiers`` to the
            complement of the lexicon spans.
    """
    if not text:
        return text
    if spell:
        return spell_text(text)
    if not mapping and not expand_identifiers:
        return text
    rewritten, spans = apply_lexicon(text, mapping or {})
    if expand_identifiers:
        rewritten = expand_code_identifiers(rewritten, protected=spans)
    return rewritten
