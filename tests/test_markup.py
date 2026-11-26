"""Contract tests for inline expressive markup: :mod:`agent_voice.markup`.

Every assertion here is a pure-parser one. ``parse_markup`` never touches a
worker, an engine or the filesystem, which is what lets the caps and the
malformed-value checks be admission-time guarantees ("before any GPU work").
"""

from __future__ import annotations

import pytest

from agent_voice import text_norm
from agent_voice.code_norm import spell_identifiers, spell_out
from agent_voice.markup import (
    DEFAULT_PAUSE_MS,
    EMPHASIS_FACTOR,
    EMPHASIS_GAIN_DB,
    FAST_FACTOR,
    MAX_DEPTH,
    MAX_PAUSE_MS,
    MAX_SEGMENTS,
    MAX_SPEED,
    MAX_TAGS,
    MIN_SPEED,
    PAUSE_FORMS,
    SLOW_FACTOR,
    MarkupScript,
    MarkupSegment,
    parse_markup,
    strip_markup,
)
from agent_voice.schemas import AgentVoiceError, ErrorCode


def _speech(script: MarkupScript) -> list[MarkupSegment]:
    return [segment for segment in script.segments if not segment.is_pause]


def _silences(script: MarkupScript) -> list[int]:
    return [segment.silence_ms for segment in script.segments if segment.is_pause]


def _reject(text: str, *, paragraph_gap_ms: int = 0) -> AgentVoiceError:
    with pytest.raises(AgentVoiceError) as excinfo:
        parse_markup(text, paragraph_gap_ms=paragraph_gap_ms)
    error = excinfo.value
    assert error.error_code == ErrorCode.BAD_REQUEST
    assert error.http_status == 400
    return error


# ---------------------------------------------------------------------------
# [pause]: the documented forms, the 0..10 s bounds and the malformed values
# ---------------------------------------------------------------------------


def test_bare_pause_is_400ms():
    script = parse_markup("a[pause]b")

    assert DEFAULT_PAUSE_MS == 400
    assert _silences(script) == [400]
    assert [segment.text for segment in _speech(script)] == ["a", "b"]
    pause = next(segment for segment in script.segments if segment.is_pause)
    assert pause.origin == "tag"


@pytest.mark.parametrize(
    ("tag", "expected_ms"),
    [
        ("[pause 500ms]", 500),
        ("[pause 250MS]", 250),
        ("[pause 1.5s]", 1500),
        ("[pause 1.25s]", 1250),
        ("[pause 1S]", 1000),
        ("[pause 10s]", 10_000),
        ("[pause 10000ms]", 10_000),
        ("[pause 10 s]", 10_000),
    ],
)
def test_pause_accepts_ms_and_seconds(tag: str, expected_ms: int):
    assert MAX_PAUSE_MS == 10_000

    assert _silences(parse_markup(f"a{tag}b")) == [expected_ms]


@pytest.mark.parametrize("tag", ["[pause 0ms]", "[pause 0s]", "[pause 0.0005s]"])
def test_zero_length_pause_emits_no_segment(tag: str):
    script = parse_markup(f"a{tag}b")

    assert _silences(script) == []
    assert [segment.text for segment in script.segments] == ["ab"]
    assert script.had_markup is True


@pytest.mark.parametrize(
    "tag", ["[pause abc]", "[pause 500]", "[pause 1e3ms]", "[pause 1,5s]"]
)
def test_non_duration_pause_is_bad_request(tag: str):
    error = _reject(f"a{tag}b")

    assert "malformed markup tag" in error.message
    assert tag in error.message
    assert "not a duration" in error.message
    assert PAUSE_FORMS in error.suggestion
    assert "markup=False" in error.suggestion


@pytest.mark.parametrize("tag", ["[pause -1s]", "[pause -0.5s]"])
def test_negative_pause_is_bad_request(tag: str):
    error = _reject(f"a{tag}b")

    assert "malformed markup tag" in error.message
    assert "a pause cannot be negative" in error.message
    assert PAUSE_FORMS in error.suggestion


@pytest.mark.parametrize("tag", ["[pause 99s]", "[pause 10.001s]", "[pause 10001ms]"])
def test_pause_longer_than_ten_seconds_is_bad_request(tag: str):
    error = _reject(f"a{tag}b")

    assert "malformed markup tag" in error.message
    assert "a pause cannot exceed 10 seconds" in error.message
    assert PAUSE_FORMS in error.suggestion


@pytest.mark.parametrize(
    "text", ["a[pause abc]b", "[pause -1s]", "[pause 99s]", "[pause 1e3ms]"]
)
def test_markup_false_speaks_malformed_pauses_verbatim(text: str):
    script = parse_markup(text, markup=False)

    assert script.display_text == text
    assert len(script.segments) == 1
    assert script.segments[0].text == text


@pytest.mark.parametrize(
    ("tag", "expected_ms"),
    [("[Pause 500ms]", 500), ("[PAUSE 1.5s]", 1500)],
)
def test_pause_tag_name_is_case_insensitive(tag: str, expected_ms: int):
    # Source behaviour: _read_tag() lower-cases every name, so [Pause] parses.
    # docs/troubleshooting.md calls [Pause 500ms] literal; that note is stale.
    assert _silences(parse_markup(f"a{tag}b")) == [expected_ms]


# ---------------------------------------------------------------------------
# [slow] / [fast] / [emphasis] / [spell]: delivery, nesting and clamping
# ---------------------------------------------------------------------------


def test_documented_delivery_constants():
    assert SLOW_FACTOR == 0.82
    assert FAST_FACTOR == 1.22
    assert EMPHASIS_FACTOR == 0.94
    assert EMPHASIS_GAIN_DB == 2.5


@pytest.mark.parametrize(
    ("text", "speed", "gain_db", "spell"),
    [
        ("[slow]x[/slow]", SLOW_FACTOR, 0.0, False),
        ("[fast]x[/fast]", FAST_FACTOR, 0.0, False),
        ("[emphasis]x[/emphasis]", EMPHASIS_FACTOR, EMPHASIS_GAIN_DB, False),
        ("[spell]x[/spell]", 1.0, 0.0, True),
    ],
)
def test_paired_tags_set_delivery(
    text: str, speed: float, gain_db: float, spell: bool
):
    script = parse_markup(text)

    assert len(script.segments) == 1
    segment = script.segments[0]
    assert segment.text == "x"
    assert segment.speed == pytest.approx(speed)
    assert segment.gain_db == pytest.approx(gain_db)
    assert segment.spell is spell
    assert script.display_text == "x"
    assert script.had_markup is True
    assert script.warnings == ()


def test_speed_multipliers_multiply_when_nested():
    script = parse_markup("[slow][fast]x[/fast][/slow]")

    assert script.segments[0].speed == pytest.approx(SLOW_FACTOR * FAST_FACTOR)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[slow][slow]x[/slow][/slow]", SLOW_FACTOR**2),
        ("[slow][slow][slow]x[/slow][/slow][/slow]", SLOW_FACTOR**3),
        ("[slow][slow][slow][slow]x[/slow][/slow][/slow][/slow]", MIN_SPEED),
        ("[fast][fast][fast][fast]x[/fast][/fast][/fast][/fast]", MAX_SPEED),
    ],
)
def test_nested_speed_is_clamped(text: str, expected: float):
    assert MIN_SPEED == 0.5
    assert MAX_SPEED == 2.0

    assert parse_markup(text).segments[0].speed == pytest.approx(expected)


def test_deepest_supported_nesting_combines_every_modifier():
    text = "[slow][fast][emphasis][spell]x[/spell][/emphasis][/fast][/slow]"
    segment = parse_markup(text).segments[0]

    assert segment.speed == pytest.approx(SLOW_FACTOR * FAST_FACTOR * EMPHASIS_FACTOR)
    assert segment.gain_db == pytest.approx(EMPHASIS_GAIN_DB)
    assert segment.spell is True


def test_nested_emphasis_gains_add_up():
    segment = parse_markup("[emphasis][emphasis]x[/emphasis][/emphasis]").segments[0]

    assert segment.speed == pytest.approx(EMPHASIS_FACTOR**2)
    assert segment.gain_db == pytest.approx(2 * EMPHASIS_GAIN_DB)


def test_spell_inside_slow_keeps_both_modifiers():
    segment = parse_markup("[slow][spell]v0.4.0[/spell][/slow]").segments[0]

    assert segment.speed == pytest.approx(SLOW_FACTOR)
    assert segment.spell is True
    assert segment.gain_db == 0.0


@pytest.mark.parametrize(
    ("text", "speed", "gain_db", "spell"),
    [
        ("[SLOW]x[/SLOW]", SLOW_FACTOR, 0.0, False),
        ("[Fast]x[/fAST]", FAST_FACTOR, 0.0, False),
        ("[EmPhAsIs]x[/EMPHASIS]", EMPHASIS_FACTOR, EMPHASIS_GAIN_DB, False),
        ("[SPELL]x[/Spell]", 1.0, 0.0, True),
    ],
)
def test_paired_tag_names_are_case_insensitive(
    text: str, speed: float, gain_db: float, spell: bool
):
    segment = parse_markup(text).segments[0]

    assert segment.speed == pytest.approx(speed)
    assert segment.gain_db == pytest.approx(gain_db)
    assert segment.spell is spell


@pytest.mark.parametrize("text", ["[slow 5]", "[fast 1]", "[emphasis loud]", "[spell 2]"])
def test_paired_tags_reject_a_value(text: str):
    error = _reject(text)

    assert "malformed markup tag" in error.message
    assert "takes no value" in error.message
    assert "markup=False" in error.suggestion


@pytest.mark.parametrize("text", ["[/slow 5]", "[/fast x]", "[/emphasis 1]", "[/spell 2]"])
def test_closing_tags_reject_a_value(text: str):
    error = _reject(text)

    assert "malformed markup tag" in error.message
    assert "a closing tag takes no value" in error.message


# ---------------------------------------------------------------------------
# Only the whitelist is a tag; everything else survives verbatim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "arr[0]",
        "matrix[i][j]",
        "[a, b]",
        "[1, 2]",
        "[pausex]",
        "[]",
        "[[x]]",
        "[div]",
        "[/div]",
        "[pauseabc]",
        "plain [ text",
        "a  b",
    ],
)
def test_non_tag_brackets_are_literal_and_keep_display_text(text: str):
    script = parse_markup(text)

    assert script.display_text == text
    assert script.had_markup is False
    assert script.warnings == ()
    assert len(script.segments) == 1
    assert script.segments[0].text == text
    assert script.segments[0].display_text == text
    assert strip_markup(text) == text


def test_literal_bracket_survives_next_to_a_real_tag():
    script = parse_markup("x [a, b] y [pause] z")

    assert _silences(script) == [400]
    assert [segment.text for segment in _speech(script)] == ["x [a, b] y", "z"]
    assert script.display_text == "x [a, b] y z"


def test_whitelisted_tag_inside_literal_brackets_still_parses():
    # The parser deliberately rescans after a non-tag "[" (see _Parser.run),
    # so a real tag wrapped in extra brackets still fires; [[x]] stays literal.
    # docs/troubleshooting.md calls "[[pause]]" literal, which the source does not.
    script = parse_markup("[[pause]]")

    assert script.display_text == "[]"
    assert _silences(script) == [400]
    assert parse_markup("[[x]]").display_text == "[[x]]"


def test_literal_brackets_do_not_count_against_the_tag_cap():
    text = "[pausex]" * 500
    script = parse_markup(text)

    assert script.had_markup is False
    assert len(script.segments) == 1
    assert script.segments[0].text == text


# ---------------------------------------------------------------------------
# Warnings, never errors, for unbalanced tags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("opener", "speed", "gain_db", "spell"),
    [
        ("[slow]", SLOW_FACTOR, 0.0, False),
        ("[fast]", FAST_FACTOR, 0.0, False),
        ("[emphasis]", EMPHASIS_FACTOR, EMPHASIS_GAIN_DB, False),
        ("[spell]", 1.0, 0.0, True),
    ],
)
def test_unclosed_opener_auto_closes_with_a_warning(
    opener: str, speed: float, gain_db: float, spell: bool
):
    script = parse_markup(f"{opener}word")

    assert script.warnings == (
        f"unclosed {opener} auto-closed at the end of the utterance",
    )
    assert script.display_text == "word"
    assert len(script.segments) == 1
    segment = script.segments[0]
    assert segment.text == "word"
    assert "[" not in segment.text
    assert segment.speed == pytest.approx(speed)
    assert segment.gain_db == pytest.approx(gain_db)
    assert segment.spell is spell


def test_multiple_unclosed_openers_are_each_reported():
    script = parse_markup("[slow][fast]x")

    assert script.warnings == (
        "unclosed [fast] auto-closed at the end of the utterance",
        "unclosed [slow] auto-closed at the end of the utterance",
    )
    assert script.segments[0].speed == pytest.approx(SLOW_FACTOR * FAST_FACTOR)


@pytest.mark.parametrize("closer", ["[/slow]", "[/fast]", "[/emphasis]", "[/spell]"])
def test_stray_closer_is_dropped_with_a_warning(closer: str):
    script = parse_markup(f"a{closer}b")

    assert script.warnings == (f"stray {closer} dropped: nothing is open",)
    assert script.display_text == "ab"
    assert [segment.text for segment in script.segments] == ["ab"]
    assert closer not in script.display_text


def test_mismatched_closer_is_dropped_with_a_warning():
    script = parse_markup("[slow]a[/fast]b[/slow]")

    assert script.warnings == (
        "stray [/fast] dropped: innermost open tag is [slow]",
    )
    assert script.display_text == "ab"
    assert len(script.segments) == 1
    assert script.segments[0].text == "ab"
    assert script.segments[0].speed == pytest.approx(SLOW_FACTOR)


def test_unknown_closer_is_literal_not_a_stray_tag():
    script = parse_markup("a[/div]b")

    assert script.display_text == "a[/div]b"
    assert script.warnings == ()
    assert script.had_markup is False


# ---------------------------------------------------------------------------
# Structural caps: rejected by the parser, before any worker can exist
# ---------------------------------------------------------------------------


def test_tag_cap_is_checked_by_the_parser():
    assert MAX_TAGS == 128
    at_limit = "[slow]" + "[/slow]" * (MAX_TAGS - 1)
    accepted = parse_markup(at_limit)
    assert accepted.segments == ()
    assert accepted.display_text == ""

    error = _reject("[slow]" + "[/slow]" * MAX_TAGS)
    assert f"more than {MAX_TAGS} markup tags" in error.message
    assert "markup=False" in error.suggestion


def test_depth_cap_is_checked_by_the_parser():
    assert MAX_DEPTH == 4
    allowed = "[slow][fast][emphasis][spell]x[/spell][/emphasis][/fast][/slow]"
    assert len(parse_markup(allowed).segments) == 1

    error = _reject("[slow][fast][emphasis][spell][slow]x")
    assert f"deeper than {MAX_DEPTH} levels" in error.message
    assert "[slow]" in error.message


def test_segment_cap_is_checked_by_the_parser():
    assert MAX_SEGMENTS == 32
    at_limit = "".join(f"w{i}[pause]" for i in range(16))
    accepted = parse_markup(at_limit)
    assert len(accepted.segments) == MAX_SEGMENTS
    assert sum(1 for segment in accepted.segments if segment.is_pause) == 16

    error = _reject(at_limit + "end")
    assert f"limit is {MAX_SEGMENTS}" in error.message
    assert "33 segments" in error.message


# ---------------------------------------------------------------------------
# markup=False: a complete, byte-for-byte bypass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "[pause]",
        "[pause 500ms]",
        "[pause 1.5s]",
        "[slow]a[/slow]",
        "[fast]b[/fast]",
        "[emphasis]c[/emphasis]",
        "[spell]d[/spell]",
        "arr[0]",
        "matrix[i][j]",
        "[a, b]",
        "[1, 2]",
        "[pausex]",
        "[]",
        "[[x]]",
        "[pause abc]",
        "a\n\nb",
        "  leading and trailing  ",
        "[pause][slow]a[/slow]arr[0]\n\n[x]",
    ],
)
def test_markup_false_round_trips_every_tag_and_bracket(text: str):
    script = parse_markup(text, markup=False)

    assert script.raw_text == text
    assert script.display_text == text
    assert script.had_markup is False
    assert script.warnings == ()
    assert len(script.segments) == 1
    segment = script.segments[0]
    assert segment.kind == "speech"
    assert segment.origin == "raw"
    assert segment.text == text
    assert segment.display_text == text
    assert segment.speed == 1.0
    assert segment.gain_db == 0.0
    assert segment.spell is False


def test_markup_false_keeps_empty_text_empty():
    script = parse_markup("", markup=False)

    assert script.segments == ()
    assert script.raw_text == ""
    assert script.display_text == ""


def test_markup_false_skips_paragraph_gaps():
    text = "one\n\ntwo"
    script = parse_markup(text, markup=False, paragraph_gap_ms=500)

    assert _silences(script) == []
    assert len(script.segments) == 1
    assert script.segments[0].text == text


# ---------------------------------------------------------------------------
# paragraph_gap_ms: exact silences, between runs only
# ---------------------------------------------------------------------------


def test_paragraph_gap_zero_collapses_a_blank_line_to_a_space():
    script = parse_markup("one\n\ntwo", paragraph_gap_ms=0)

    assert _silences(script) == []
    assert len(script.segments) == 1
    assert script.segments[0].text == "one two"
    assert script.display_text == "one\n\ntwo"


@pytest.mark.parametrize("blank", ["\n\n", "\n\n\n\n", "\n \n"])
def test_paragraph_gap_zero_collapses_any_blank_run(blank: str):
    script = parse_markup(f"one{blank}two", paragraph_gap_ms=0)

    assert _silences(script) == []
    assert script.segments[0].text == "one two"


@pytest.mark.parametrize("gap", [1, 250, 500, 5000])
def test_paragraph_gap_inserts_exact_silence_between_runs(gap: int):
    script = parse_markup("one\n\ntwo", paragraph_gap_ms=gap)

    assert [segment.kind for segment in script.segments] == [
        "speech",
        "pause",
        "speech",
    ]
    assert _silences(script) == [gap]
    pause = next(segment for segment in script.segments if segment.is_pause)
    assert pause.origin == "paragraph"
    assert [segment.text for segment in _speech(script)] == ["one", "two"]


def test_repeated_blank_lines_make_one_paragraph_pause():
    script = parse_markup("a\n\n\n\nb", paragraph_gap_ms=500)

    assert _silences(script) == [500]
    assert [segment.text for segment in _speech(script)] == ["a", "b"]


@pytest.mark.parametrize(
    "text", ["\n\nfoo", "foo\n\n", "   \n\nfoo", "foo\n\n   ", "\n\n"]
)
def test_paragraph_pause_never_leads_or_trails(text: str):
    script = parse_markup(text, paragraph_gap_ms=500)

    assert _silences(script) == []


def test_each_interior_paragraph_break_gets_its_own_pause():
    script = parse_markup("a\n\nb\n\nc", paragraph_gap_ms=250)

    assert [segment.kind for segment in script.segments] == [
        "speech",
        "pause",
        "speech",
        "pause",
        "speech",
    ]
    assert _silences(script) == [250, 250]
    assert [segment.text for segment in _speech(script)] == ["a", "b", "c"]


def test_paragraph_gap_applies_inside_a_paired_tag():
    script = parse_markup("[slow]a\n\nb[/slow]", paragraph_gap_ms=500)

    assert _silences(script) == [500]
    assert [segment.text for segment in _speech(script)] == ["a", "b"]
    assert all(segment.speed == pytest.approx(SLOW_FACTOR) for segment in _speech(script))


def test_explicit_pause_at_the_edges_is_kept():
    leading = parse_markup("[pause] hi")
    trailing = parse_markup("hi[pause]")

    assert leading.segments[0].is_pause
    assert leading.segments[0].silence_ms == 400
    assert leading.segments[0].origin == "tag"
    assert trailing.segments[-1].is_pause
    assert trailing.segments[-1].silence_ms == 400
    assert trailing.segments[-1].origin == "tag"


def test_paragraph_gap_bounds_are_enforced():
    for gap in (0, 5000):
        assert parse_markup("x", paragraph_gap_ms=gap).raw_text == "x"

    for gap in (-1, 5001):
        error = _reject("x", paragraph_gap_ms=gap)
        assert "paragraph_gap_ms" in error.message
        assert "between 0 and 5000" in error.message


def test_paragraph_gap_must_be_an_integer():
    with pytest.raises(AgentVoiceError) as excinfo:
        parse_markup("x", paragraph_gap_ms="abc")  # type: ignore[arg-type]

    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    assert "must be an integer" in excinfo.value.message


# ---------------------------------------------------------------------------
# display_text / strip_markup: the subtitle view
# ---------------------------------------------------------------------------


def test_display_text_strips_tags_but_keeps_written_words():
    text = "Ship it [pause 400ms] — [emphasis]read the diff[/emphasis] first."
    script = parse_markup(text)

    assert script.display_text == "Ship it — read the diff first."
    assert _silences(script) == [400]
    emphasized = [segment for segment in _speech(script) if segment.gain_db > 0]
    assert [segment.text for segment in emphasized] == ["read the diff"]
    assert emphasized[0].speed == pytest.approx(EMPHASIS_FACTOR)
    assert "[" not in script.display_text
    assert "]" not in script.display_text


def test_display_text_collapses_the_gap_a_tag_leaves_behind():
    assert parse_markup("a [pause] b").display_text == "a b"


def test_spacing_is_preserved_when_no_tag_was_consumed():
    text = "a  b [1, 2]"
    script = parse_markup(text)

    assert script.display_text == text
    assert script.segments[0].text == text


def test_spell_block_display_keeps_the_written_words():
    script = parse_markup("[spell]HTTPServer[/spell]")

    assert script.display_text == "HTTPServer"
    assert script.speech[0].spell is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[slow]a[/slow] b [pause 500ms] c", "a b c"),
        ("[spell]HTTPServer[/spell]", "HTTPServer"),
        ("arr[0] and [1, 2]", "arr[0] and [1, 2]"),
        ("[pausex] [a, b]", "[pausex] [a, b]"),
        ("a\n\nb", "a\n\nb"),
        ("", ""),
    ],
)
def test_strip_markup_returns_the_display_form(text: str, expected: str):
    assert strip_markup(text) == expected
    assert strip_markup(text) == parse_markup(text).display_text


def test_strip_markup_keeps_the_source_layout_for_paragraphs():
    assert strip_markup("a\n\nb", paragraph_gap_ms=500) == "a\n\nb"


def test_strip_markup_rejects_a_malformed_pause():
    with pytest.raises(AgentVoiceError) as excinfo:
        strip_markup("[pause abc]")

    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST


# ---------------------------------------------------------------------------
# [spell] through text_norm: spelled once, protected from both later layers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("v0.4.0", "V zero dot four dot zero"),
        ("API", "A P I"),
        ("TCP/IP", "T C P slash I P"),
    ],
)
def test_spell_text_spells_each_character(text: str, expected: str):
    assert text_norm.spell_text(text) == expected
    assert text_norm.spell_text(text) == spell_out(text)
    assert text_norm.normalize_text(text, spell=True) == expected


def test_spell_block_spells_each_run_once():
    script = parse_markup("[spell]v0.4.0[/spell] and [spell]TCP/IP[/spell]")

    assert script.display_text == "v0.4.0 and TCP/IP"
    assert [segment.spell for segment in script.speech] == [True, False, True]
    spoken = [
        text_norm.normalize_text(
            segment.text,
            mapping={"v0.4.0": "never", "tcp/ip": "never"},
            spell=segment.spell,
            expand_identifiers=True,
        )
        for segment in script.speech
    ]

    assert spoken == ["V zero dot four dot zero", "and", "T C P slash I P"]
    assert spoken[0].count("zero") == 2
    assert "version" not in spoken[0]
    assert spoken[2] == spell_out("TCP/IP")


def test_spell_block_is_protected_from_lexicon_and_code_mode():
    script = parse_markup("[spell]HTTPServer[/spell]")
    segment = script.speech[0]
    spoken = text_norm.normalize_text(
        segment.text,
        mapping={"HTTPServer": "the server"},
        spell=segment.spell,
        expand_identifiers=True,
    )

    assert spoken == "H T T P S E R V E R"
    assert spoken == spell_out(segment.text)
    # One token per glyph, exactly once (spell_out upper-cases letters).
    assert spoken.split() == [character.upper() for character in "HTTPServer"]
    assert spoken != spell_identifiers(segment.text)
    assert "HTTP" not in spoken


def test_spell_block_ignores_whitespace_between_glyphs():
    assert text_norm.spell_text("TCP / IP") == text_norm.spell_text("TCP/IP")

    script = parse_markup("[spell]TCP / IP[/spell]")
    assert text_norm.normalize_text(
        script.speech[0].text, spell=script.speech[0].spell
    ) == ("T C P slash I P")


# ---------------------------------------------------------------------------
# normalize_text ordering: lexicon, then code mode, and never twice
# ---------------------------------------------------------------------------


def test_lexicon_wins_over_code_identifiers():
    assert (
        text_norm.normalize_text(
            "kubectl get pods",
            mapping={"kubectl": "cube control"},
            expand_identifiers=True,
        )
        == "cube control get pods"
    )


def test_lexicon_replacement_is_never_expanded_a_second_time():
    # "cube_control" and "v0.4.0" would both be rewritten by code_norm if the
    # protected spans were ignored.
    assert (
        text_norm.normalize_text(
            "kubectl",
            mapping={"kubectl": "cube_control"},
            expand_identifiers=True,
        )
        == "cube_control"
    )
    assert (
        text_norm.normalize_text(
            "api", mapping={"api": "v0.4.0"}, expand_identifiers=True
        )
        == "v0.4.0"
    )


def test_both_layers_run_in_order_lexicon_then_code():
    assert (
        text_norm.normalize_text(
            "kubectl reads HTTPServer",
            mapping={"kubectl": "cube control"},
            expand_identifiers=True,
        )
        == "cube control reads HTTP Server"
    )


def test_code_expansion_is_opt_in():
    text = "kubectl reads HTTPServer"

    assert text_norm.normalize_text(text) == text
    assert (
        text_norm.normalize_text(text, mapping={"kubectl": "cube control"})
        == "cube control reads HTTPServer"
    )
    assert text_norm.normalize_text("worker_manager", expand_identifiers=True) == (
        "worker manager"
    )


def test_spell_bypasses_mapping_and_code_expansion():
    assert (
        text_norm.normalize_text(
            "HTTPServer",
            mapping={"HTTPServer": "the server"},
            spell=True,
            expand_identifiers=True,
        )
        == "H T T P S E R V E R"
    )
