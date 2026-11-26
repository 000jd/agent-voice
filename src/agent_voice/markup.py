"""Inline expressive markup: a tiny, whitelisted, fail-loudly tag language.

``[pause]``, ``[slow]``, ``[fast]``, ``[emphasis]`` and ``[spell]`` are the whole
vocabulary. Anything else in brackets — ``arr[0]``, ``matrix[i][j]``, ``[a, b]``,
``[1, 2]``, ``[pausex]``, ``[]``, ``[[x]]`` — is not a tag and survives verbatim,
so a line of code or a citation still speaks as written.

The distinction that matters is between *unrecognised* and *malformed*:

* ``[pausex]`` is a bracket that is not a tag at all -> literal text.
* ``[pause abc]`` / ``[pause -1s]`` / ``[pause 99s]`` are a whitelisted tag with a
  value it cannot honour -> ``BAD_REQUEST`` with the accepted forms. Silently
  narrating "pause abc" would be a confident lie about what was rendered.

Everything here is pure: :func:`parse_markup` never touches the network, a worker
or the filesystem, which is what lets the pipeline reject a malformed line or an
over-cap script *before* any GPU work is scheduled.

Shape::

    [slow]a[/slow] b [pause 500ms] c

    segments = speech("a", speed=0.82) speech("b") pause(500) speech("c")
    display_text = "a b  c"     (tags gone, words exactly as written)

``display_text`` is what subtitles and ``BeatTiming.text`` report; the rewritten
spoken form is derived later (see :mod:`agent_voice.text_norm`) because it needs
the resolved voice for the scoped lexicon.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .schemas import AgentVoiceError, ErrorCode

MAX_TAGS = 128
"""Total tags (openers, closers and pauses) allowed in one utterance."""

MAX_DEPTH = 4
"""How deeply paired tags may nest. ``[slow][fast][emphasis][spell]`` is 4."""

MAX_SEGMENTS = 32
"""Speech + pause segments one utterance may expand into (== synthesis calls)."""

DEFAULT_PAUSE_MS = 400
"""``[pause]`` with no value means a 400 ms beat."""

MAX_PAUSE_MS = 10_000
"""``[pause 10s]`` is the longest single pause; anything longer is a script bug."""

SLOW_FACTOR = 0.82
FAST_FACTOR = 1.22
EMPHASIS_FACTOR = 0.94
EMPHASIS_GAIN_DB = 2.5
"""``[emphasis]`` leans on the words: a touch slower and 2.5 dB louder.

The gain is deliberately modest and is applied to the segment audio at stitch
time, after the per-segment cache, with a peak limit — it must read as emphasis,
not as a limiter artefact, and it has to survive the master loudness stage.
"""

MIN_SPEED = 0.5
MAX_SPEED = 2.0
"""Nested speed multipliers are clamped to the range every engine can speak."""

PAUSE_FORMS = "[pause], [pause 500ms] or [pause 1.5s] (0 to 10 seconds)"
"""The accepted pause spellings, quoted verbatim in every error and suggestion."""

_PAIRED: dict[str, tuple[float, float]] = {
    "slow": (SLOW_FACTOR, 0.0),
    "fast": (FAST_FACTOR, 0.0),
    "emphasis": (EMPHASIS_FACTOR, EMPHASIS_GAIN_DB),
    "spell": (1.0, 0.0),
}
"""name -> (speed multiplier, gain in dB). ``[spell]`` changes no delivery."""

_KNOWN = frozenset(_PAIRED) | {"pause"}

_TAG_NAME = re.compile(r"([A-Za-z]+)")
_PAUSE_VALUE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(ms|s)", re.IGNORECASE)

_PARAGRAPH_BREAK = re.compile(r"(?:[ \t]*\r?\n){2,}[ \t]*")
"""A blank line: two newlines with only indentation between them."""

_MULTI_SPACE = re.compile(r"[ \t]{2,}")


@dataclass(frozen=True)
class MarkupSegment:
    """One unit of work for the synthesizer: a speech run or a pause."""

    kind: Literal["speech", "pause"] = "speech"
    text: str = ""
    """Written text of the run (spelling and lexicon rewriting happen later)."""
    display_text: str = ""
    """The same run with no tags, for subtitles: ``[spell]`` words stay written."""
    speed: float = 1.0
    """Multiplier contributed by enclosing ``[slow]``/``[fast]``/``[emphasis]``."""
    gain_db: float = 0.0
    """Gain contributed by enclosing ``[emphasis]`` tags, applied at stitch time."""
    spell: bool = False
    """True inside a ``[spell]`` block: spell the characters, skip lexicon/code."""
    silence_ms: int = 0
    origin: str = "text"
    """Where a pause came from: ``tag``, ``paragraph``, or ``text`` for speech."""

    @property
    def is_pause(self) -> bool:
        return self.kind == "pause"


@dataclass(frozen=True)
class MarkupScript:
    """The parsed form of one utterance, ready for normalization."""

    raw_text: str
    segments: tuple[MarkupSegment, ...]
    display_text: str
    warnings: tuple[str, ...] = ()
    had_markup: bool = False
    """True when at least one recognised tag was consumed."""

    @property
    def speech(self) -> tuple[MarkupSegment, ...]:
        return tuple(segment for segment in self.segments if not segment.is_pause)


@dataclass(frozen=True)
class _Context:
    speed: float
    gain_db: float
    spell: bool


def _validate_gap(paragraph_gap_ms: int) -> int:
    """Reject an out-of-range gap before it can become unbounded silence.

    Pydantic already bounds the field on the REST/MCP models, but the WebSocket
    and stream bodies are free-form dicts, and a script beat can carry its own
    value; this is the one gate every caller passes through.
    """
    try:
        gap = int(paragraph_gap_ms)
    except (TypeError, ValueError) as exc:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"paragraph_gap_ms must be an integer, got {paragraph_gap_ms!r}",
            "Use a whole number of milliseconds between 0 and 5000.",
        ) from exc
    if gap < 0 or gap > 5000:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"paragraph_gap_ms is {gap}, but must be between 0 and 5000",
            "Use 0 to collapse paragraph breaks into a space, or up to 5000 ms.",
        )
    return gap


def _malformed_pause(raw: str, detail: str, suggestion: str = "") -> AgentVoiceError:
    return AgentVoiceError(
        ErrorCode.BAD_REQUEST,
        f"malformed markup tag {raw}: {detail}",
        suggestion or f"Use {PAUSE_FORMS}, or markup=False to speak the brackets.",
    )


def _parse_pause_ms(value: str, *, raw: str) -> int:
    stripped = value.strip()
    if stripped.startswith("-"):
        raise _malformed_pause(raw, "a pause cannot be negative")
    match = _PAUSE_VALUE.fullmatch(stripped)
    if match is None:
        raise _malformed_pause(raw, f"{value!r} is not a duration")
    amount = float(match.group(1))
    unit = match.group(2).lower()
    millis = amount / 1000.0 if unit == "ms" else amount
    millis = round(millis * 1000)
    if millis > MAX_PAUSE_MS:
        raise _malformed_pause(
            raw, f"a pause cannot exceed {MAX_PAUSE_MS // 1000} seconds"
        )
    return int(millis)


def _read_tag(content: str) -> tuple[Literal["open", "close"], str, str] | None:
    """Classify one bracketed span, or ``None`` when it is not a tag at all."""
    body = content
    closing = body.startswith("/")
    if closing:
        body = body[1:]
    match = _TAG_NAME.match(body)
    if match is None:
        return None
    name = match.group(1).lower()
    if name not in _KNOWN:
        # [pausex], [a, b], [div], [i] ... not our vocabulary: literal text.
        return None
    return ("close" if closing else "open"), name, body[match.end() :].strip()


def _tag_forms(name: str) -> str:
    if name == "pause":
        return f"Use {PAUSE_FORMS}."
    return f"Use [{name}]...[/{name}] with no value, or markup=False to speak the brackets."


class _Parser:
    """Single pass over the raw text; collects segments, display text, warnings."""

    def __init__(self, text: str, *, paragraph_gap_ms: int) -> None:
        self.text = text
        self.gap = paragraph_gap_ms
        self.stack: list[str] = []
        self.current: list[str] = []
        self.display: list[str] = []
        self.segments: list[MarkupSegment] = []
        self.warnings: list[str] = []
        self.tag_count = 0
        self.had_markup = False

    # -- literal text ---------------------------------------------------
    def literal(self, raw: str) -> None:
        if not raw:
            return
        # Display keeps the caller's bytes: a line with no tags must round-trip.
        self.display.append(raw)
        if self.gap <= 0:
            # Paragraph breaks collapse to a space so the synthesizer hears one
            # continuous sentence instead of a newline it may read as a full stop.
            self.current.append(_PARAGRAPH_BREAK.sub(" ", raw))
            return
        position = 0
        for match in _PARAGRAPH_BREAK.finditer(raw):
            head = raw[position : match.start()]
            if head:
                self.current.append(head)
            self.flush()
            self.segments.append(
                MarkupSegment(
                    kind="pause", silence_ms=self.gap, origin="paragraph"
                )
            )
            position = match.end()
        tail = raw[position:]
        if tail:
            self.current.append(tail)

    # -- speech buffer --------------------------------------------------
    def context(self) -> _Context:
        speed = 1.0
        gain = 0.0
        spell = False
        for name in self.stack:
            factor, gain_db = _PAIRED[name]
            speed *= factor
            gain += gain_db
            spell = spell or name == "spell"
        return _Context(
            speed=min(MAX_SPEED, max(MIN_SPEED, speed)),
            gain_db=round(gain, 4),
            spell=spell,
        )

    def flush(self) -> None:
        if not self.current:
            return
        body = "".join(self.current)
        self.current.clear()
        if not body:
            return
        context = self.context()
        self.segments.append(
            MarkupSegment(
                kind="speech",
                text=body,
                display_text=body,
                speed=context.speed,
                gain_db=context.gain_db,
                spell=context.spell,
            )
        )

    # -- tags -----------------------------------------------------------
    def tag(
        self,
        kind: Literal["open", "close"],
        name: str,
        value: str,
        *,
        raw: str,
    ) -> None:
        self.tag_count += 1
        if self.tag_count > MAX_TAGS:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"text contains more than {MAX_TAGS} markup tags",
                "Split the line into shorter utterances, remove tags, or pass "
                "markup=False to speak the brackets literally.",
            )
        self.had_markup = True
        self.flush()

        if kind == "close":
            if value:
                raise AgentVoiceError(
                    ErrorCode.BAD_REQUEST,
                    f"malformed markup tag {raw}: a closing tag takes no value",
                    _tag_forms(name),
                )
            if name not in _PAIRED:
                self.warnings.append(
                    f"stray {raw} dropped: [{name}] has no closing form"
                )
                return
            if not self.stack:
                self.warnings.append(f"stray {raw} dropped: nothing is open")
                return
            if self.stack[-1] != name:
                self.warnings.append(
                    f"stray {raw} dropped: innermost open tag is "
                    f"[{self.stack[-1]}]"
                )
                return
            self.stack.pop()
            return

        if name == "pause":
            silence_ms = (
                _parse_pause_ms(value, raw=raw) if value else DEFAULT_PAUSE_MS
            )
            if silence_ms > 0:
                self.segments.append(
                    MarkupSegment(kind="pause", silence_ms=silence_ms, origin="tag")
                )
            return

        if value:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"malformed markup tag {raw}: [{name}] takes no value",
                _tag_forms(name),
            )
        if len(self.stack) >= MAX_DEPTH:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"markup nesting is deeper than {MAX_DEPTH} levels at {raw}",
                "Flatten the tags: [slow][fast][emphasis][spell] is the deepest "
                "nesting the renderer accepts.",
            )
        self.stack.append(name)

    # -- driver ---------------------------------------------------------
    def run(self) -> MarkupScript:
        text = self.text
        index = 0
        while True:
            start = text.find("[", index)
            if start < 0:
                self.literal(text[index:])
                break
            if start > index:
                self.literal(text[index:start])
            end = text.find("]", start + 1)
            if end < 0:
                # An unbracketed "[" is ordinary text, not an unclosed tag.
                self.literal(text[start:])
                break
            token = _read_tag(text[start + 1 : end])
            if token is None:
                # Not a tag: emit the single "[" and rescan, so the inner tag of
                # a construct like "[[slow]x[/slow]]" is still recognised.
                self.literal(text[start])
                index = start + 1
                continue
            kind, name, value = token
            self.tag(kind, name, value, raw=text[start : end + 1])
            index = end + 1

        self.flush()
        for name in reversed(self.stack):
            self.warnings.append(
                f"unclosed [{name}] auto-closed at the end of the utterance"
            )
        self.stack.clear()

        segments = _finalise(self.segments)
        if len(segments) > MAX_SEGMENTS:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"markup expands this utterance into {len(segments)} segments, "
                f"limit is {MAX_SEGMENTS}",
                "Reduce the number of pauses and speed changes, or split the line.",
            )

        display = "".join(self.display)
        if self.had_markup:
            # Only tidy the hole a tag left behind; a line without tags keeps
            # every space the caller wrote.
            display = _MULTI_SPACE.sub(" ", display)

        return MarkupScript(
            raw_text=text,
            segments=tuple(segments),
            display_text=display,
            warnings=tuple(self.warnings),
            had_markup=self.had_markup,
        )


def _same_delivery(left: MarkupSegment, right: MarkupSegment) -> bool:
    return (
        left.speed == right.speed
        and left.gain_db == right.gain_db
        and left.spell == right.spell
    )


def _finalise(segments: list[MarkupSegment]) -> list[MarkupSegment]:
    """Merge what can be merged, drop the empty, trim the edges."""
    merged: list[MarkupSegment] = []
    for segment in segments:
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and not segment.is_pause
            and not previous.is_pause
            and _same_delivery(previous, segment)
        ):
            merged[-1] = MarkupSegment(
                kind="speech",
                text=previous.text + segment.text,
                display_text=previous.display_text + segment.display_text,
                speed=previous.speed,
                gain_db=previous.gain_db,
                spell=previous.spell,
                origin=previous.origin,
            )
            continue
        if (
            previous is not None
            and segment.is_pause
            and previous.is_pause
            and previous.origin == segment.origin
        ):
            merged[-1] = MarkupSegment(
                kind="pause",
                silence_ms=previous.silence_ms + segment.silence_ms,
                origin=previous.origin,
            )
            continue
        merged.append(segment)

    # A pause needs speech on both sides to be a pause. Paragraph gaps are the
    # ones the contract says never lead or trail; an explicit [pause] the caller
    # wrote at the very start or end is still hers to keep.
    cleaned: list[MarkupSegment] = []
    for index, segment in enumerate(merged):
        if not segment.is_pause:
            cleaned.append(segment)
            continue
        if segment.silence_ms <= 0:
            continue
        if segment.origin == "paragraph" and (
            not any(not s.is_pause for s in merged[:index])
            or not any(not s.is_pause for s in merged[index + 1 :])
        ):
            continue
        cleaned.append(segment)

    trimmed: list[MarkupSegment] = []
    for segment in cleaned:
        if segment.is_pause:
            trimmed.append(segment)
            continue
        text = segment.text.strip()
        if not text:
            continue
        trimmed.append(
            MarkupSegment(
                kind="speech",
                text=text,
                display_text=segment.display_text.strip(),
                speed=segment.speed,
                gain_db=segment.gain_db,
                spell=segment.spell,
                origin=segment.origin,
            )
        )
    return trimmed


def parse_markup(
    text: str,
    *,
    markup: bool = True,
    paragraph_gap_ms: int = 0,
) -> MarkupScript:
    """Parse one utterance into speech runs and pauses.

    Args:
        text: The caller's text, exactly as received.
        markup: ``False`` bypasses parsing completely: one speech segment holding
            the text verbatim (so ``display_text`` round-trips byte-for-byte and
            brackets are spoken as written). Paragraph gaps are part of markup
            interpretation, so they are skipped too.
        paragraph_gap_ms: Silence to insert at each blank-line break. ``0``
            collapses a paragraph break into a single space instead.

    Raises:
        AgentVoiceError: ``BAD_REQUEST`` for a malformed tag value or an exceeded
            cap; both are checked here, before any worker can be spawned.
    """
    gap = _validate_gap(paragraph_gap_ms)
    if not markup:
        segments: tuple[MarkupSegment, ...] = ()
        if text:
            segments = (
                MarkupSegment(
                    kind="speech", text=text, display_text=text, origin="raw"
                ),
            )
        return MarkupScript(
            raw_text=text,
            segments=segments,
            display_text=text,
            warnings=(),
            had_markup=False,
        )
    return _Parser(text, paragraph_gap_ms=gap).run()


def strip_markup(text: str, *, paragraph_gap_ms: int = 0) -> str:
    """The display form of ``text``: tags removed, ``[spell]`` words kept.

    Used for subtitles. It is not a rewriting step — the words are untouched —
    so it can never change what is spoken.
    """
    return parse_markup(text, markup=True, paragraph_gap_ms=paragraph_gap_ms).display_text
