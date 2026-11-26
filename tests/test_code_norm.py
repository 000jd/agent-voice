"""Contract tests for deterministic code normalization: :mod:`agent_voice.code_norm`.

The three load-bearing properties are exercised directly: protected spans are
copied byte for byte, every rule is idempotent, and the output is bounded.
"""

from __future__ import annotations

import pytest

from agent_voice.code_norm import (
    MAX_CODE_CANDIDATES,
    MAX_EXPANSION_FACTOR,
    MAX_OUTPUT_CHARS,
    MIN_OUTPUT_BUDGET,
    code_candidates,
    spell_identifiers,
    spell_out,
)
from agent_voice.pronunciations import apply_pronunciations_spans

# ---------------------------------------------------------------------------
# spell_identifiers: the documented table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("worker_manager", "worker manager"),
        ("useState", "use State"),
        ("GuardMiddleware", "Guard Middleware"),
        ("HTTPServer", "HTTP Server"),
        ("iOS", "i O S"),
        ("v0.4.0", "version zero point four dot zero"),
        ("3.12.1", "three point twelve point one"),
        ("src/app.py", "src slash app dot py"),
        ("C++", "C plus plus"),
        ("a_b_c", "a b c"),
        ("app.py", "app dot py"),
        ("worker_manager -> HTTPServer", "worker manager to HTTP Server"),
    ],
)
def test_spell_identifiers_matches_the_documented_table(written: str, spoken: str):
    assert spell_identifiers(written) == spoken


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("a -> b", "a to b"),
        ("a->b", "a to b"),
        ("a => b", "a maps to b"),
        ("a=>b", "a maps to b"),
        ("->", " to "),
        ("=>", " maps to "),
    ],
)
def test_operators_become_words(written: str, spoken: str):
    assert spell_identifiers(written) == spoken


@pytest.mark.parametrize(
    "written",
    [
        "arr[0]",
        "matrix[i][j]",
        "[1, 2]",
        "3.5 seconds",
        "graph.h",
        "e.g. this",
        "kubectl get pods",
        "x = y",
    ],
)
def test_prose_and_indexing_are_left_alone(written: str):
    assert spell_identifiers(written) == written


@pytest.mark.parametrize(
    "written",
    [
        "worker_manager",
        "useState",
        "GuardMiddleware",
        "HTTPServer",
        "iOS",
        "v0.4.0",
        "3.12.1",
        "src/app.py",
        "a -> b",
        "a=>b",
        "C++",
        "a_b_c",
        "worker_manager -> HTTPServer",
        "v0.4.0 and HTTPServer on src/app.py",
    ],
)
def test_expansion_is_idempotent(written: str):
    once = spell_identifiers(written)

    assert spell_identifiers(once) == once


# ---------------------------------------------------------------------------
# Protected spans: what the lexicon already rewrote is untouchable
# ---------------------------------------------------------------------------


def test_protected_span_is_copied_byte_for_byte():
    text = "alpha HTTPServer beta"
    start = text.index("HTTPServer")
    span = (start, start + len("HTTPServer"))

    assert spell_identifiers(text, [span]) == text
    assert spell_identifiers(text) == "alpha HTTP Server beta"


def test_only_the_complement_of_a_protected_span_is_rewritten():
    text = "alpha HTTPServer beta"

    # "alpha " is protected; the identifier after it still expands.
    assert spell_identifiers(text, [(0, 6)]) == "alpha HTTP Server beta"


def test_multiple_protected_spans_are_all_honoured():
    text = "v0.4.0 and src/app.py"
    spans = [(0, len("v0.4.0")), (text.index("src/app.py"), len(text))]

    assert spell_identifiers(text, spans) == text
    assert spell_identifiers(text) == (
        "version zero point four dot zero and src slash app dot py"
    )


def test_spans_are_clamped_sorted_and_merged():
    # Out of range clamps to the whole string, which protects everything.
    assert spell_identifiers("HTTPServer", [(-100, 500)]) == "HTTPServer"
    # An empty span protects nothing.
    assert spell_identifiers("HTTPServer", [(3, 3)]) == "HTTP Server"
    # Overlapping spans protect their union.
    assert spell_identifiers("HTTPServer", [(2, 4), (0, 10)]) == "HTTPServer"
    # Order on the way in does not matter.
    assert spell_identifiers("a_b c_d", [(4, 7), (0, 3)]) == "a_b c_d"
    assert spell_identifiers("a_b c_d", [(0, 3)]) == "a_b c d"


def test_lexicon_spans_from_pronunciations_protect_the_replacement():
    rewritten, spans = apply_pronunciations_spans("kubectl", {"kubectl": "v0.4.0"})

    assert rewritten == "v0.4.0"
    assert spell_identifiers(rewritten, spans) == "v0.4.0"
    assert spell_identifiers(rewritten) == "version zero point four dot zero"


# ---------------------------------------------------------------------------
# Bounded output
# ---------------------------------------------------------------------------


def test_budget_constants():
    assert MAX_EXPANSION_FACTOR == 8
    assert MIN_OUTPUT_BUDGET == 4096
    assert MAX_OUTPUT_CHARS == 200_000


def test_output_stays_inside_the_expansion_budget():
    # "/" expands to "slash": about 6x, still inside max(len * 8, 4096).
    text = "/" * 30_000
    expanded = spell_identifiers(text)
    budget = min(
        max(len(text) * MAX_EXPANSION_FACTOR, MIN_OUTPUT_BUDGET), MAX_OUTPUT_CHARS
    )

    assert expanded != text
    assert len(expanded) > len(text)
    assert len(expanded) <= budget


def test_pathological_input_is_copied_verbatim_past_the_budget():
    # Expanding 40k slashes would produce ~240k characters, over the absolute
    # ceiling; the remainder is copied verbatim instead.
    text = "/" * 40_000

    assert spell_identifiers(text) == text


def test_earlier_chunks_survive_when_a_later_chunk_exceeds_the_budget():
    text = "HTTPServer " + "/" * 40_000
    # Protect the separating space so the call has two unprotected chunks; the
    # first is expanded, the second is past the budget and copied verbatim.
    output = spell_identifiers(text, [(10, 11)])

    assert output == "HTTP Server " + "/" * 40_000
    assert output.startswith("HTTP Server /")
    assert output.endswith("/" * 40_000)


# ---------------------------------------------------------------------------
# spell_out: one token per glyph
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("TCP/IP", "T C P slash I P"),
        ("v0.4.0", "V zero dot four dot zero"),
        ("API", "A P I"),
        ("HTTPServer", "H T T P S E R V E R"),
        ("42", "four two"),
        ("a b", "A B"),
        ("", ""),
        ("   ", ""),
        ("€", "€"),
        ("C++", "C plus plus"),
    ],
)
def test_spell_out_spells_every_glyph(text: str, expected: str):
    assert spell_out(text) == expected


def test_spell_out_whitespace_only_separates():
    assert spell_out("TCP / IP") == spell_out("TCP/IP")
    assert spell_out("a\tb\nc") == "A B C"


def test_spell_out_upper_cases_letters():
    assert spell_out("abc") == "A B C"


# ---------------------------------------------------------------------------
# code_candidates: the review list for `pronunciations check`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("GPU", ["GPU"]),
        ("CUDA and HTTP", ["CUDA", "HTTP"]),
        ("worker_manager", ["worker_manager"]),
        ("HTTPServer", ["HTTPServer"]),
        ("useState", ["useState"]),
        ("C++", ["C++"]),
        ("iOS", ["iOS"]),
        ("the quick brown fox", []),
        ("kubectl get pods", []),
        ("3.5 seconds", []),
        ("v0.4.0", []),
        ("", []),
    ],
)
def test_code_candidates_report_only_code_shaped_terms(text: str, expected: list[str]):
    assert code_candidates(text) == expected


def test_code_candidates_report_acronyms_identifiers_and_plus_plus():
    text = (
        "The HTTPServer serves GPU workloads via worker_manager, "
        "camelCaseName, snake_case_name and C++."
    )

    assert code_candidates(text) == [
        "C++",
        "GPU",
        "HTTPServer",
        "camelCaseName",
        "snake_case_name",
        "worker_manager",
    ]


@pytest.mark.parametrize(
    "lexicon",
    [
        {"GPU": "gee pee"},
        {"gpu": "gee pee"},
        ["GPU"],
        ("gpu",),
        "GPU",
    ],
)
def test_code_candidates_respect_the_lexicon(lexicon):
    assert code_candidates("GPU and worker_manager", lexicon) == ["worker_manager"]


def test_code_candidates_can_report_against_a_partial_lexicon():
    assert code_candidates(
        "GPU and worker_manager", {"worker_manager": "worker manager"}
    ) == ["GPU"]


def test_code_candidates_are_sorted_and_deterministic():
    text = "worker_manager HTTPServer GPU CUDA useState C++"

    first = code_candidates(text)

    assert first == code_candidates(text)
    assert first == sorted(first)
    assert len(first) == len(set(first))


def test_code_candidates_dedupe_case_insensitively_keeping_first_spelling():
    assert code_candidates("C++ c++") == ["C++"]
    assert code_candidates("c++ C++") == ["c++"]
    assert code_candidates("HTTPServer HTTPServer") == ["HTTPServer"]


def test_code_candidates_are_capped():
    text = " ".join(f"A{i:04d}" for i in range(MAX_CODE_CANDIDATES + 50))
    candidates = code_candidates(text)

    assert len(candidates) == MAX_CODE_CANDIDATES
    assert candidates == sorted(candidates)
    assert len(set(candidates)) == len(candidates)
