"""v0.3.2 pronunciation lexicon: persistence, rewriting, caps and regex cache."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_voice.pronunciations import (
    MAX_LEXICON_ENTRIES,
    MAX_LEXICON_TERM_CHARS,
    PronunciationStore,
    _compiled,
    apply_pronunciations,
    prepare_text,
    validate_pronunciation_map,
)


def test_store_set_list_and_delete(tmp_path: Path):
    store = PronunciationStore(tmp_path / "pronunciations.json")

    assert store.list() == {}
    store.set("nginx", "engine x")
    store.set("CUDA", "koo-dah")
    assert store.list() == {"CUDA": "koo-dah", "nginx": "engine x"}

    assert store.delete("cuda") is True
    assert store.list() == {"nginx": "engine x"}
    assert store.delete("missing") is False


def test_store_recasing_replaces_instead_of_duplicating(tmp_path: Path):
    store = PronunciationStore(tmp_path / "pronunciations.json")
    store.set("CUDA", "koo-dah")
    store.set("cuda", "koo-dah two")
    assert store.list() == {"cuda": "koo-dah two"}


def test_malformed_store_is_tolerated(tmp_path: Path):
    path = tmp_path / "pronunciations.json"
    path.write_text("{not json", "utf-8")
    assert PronunciationStore(path).list() == {}


def test_apply_is_case_insensitive_whole_word_and_escapes_regex():
    mapping = {"async": "ay-sink", "kubectl": "cube control", "c++": "see plus plus"}
    text = "Async code uses kubectl on C++ and asynchronously stays put."

    assert apply_pronunciations(text, mapping) == (
        "ay-sink code uses cube control on see plus plus and asynchronously stays put."
    )


def test_apply_prefers_the_longest_key():
    mapping = {"kubectl": "cube control", "kubectl pod": "cube control pod"}
    assert apply_pronunciations("kubectl pod", mapping) == "cube control pod"


def test_unicode_case_folding_matches_without_keyerror():
    # re.IGNORECASE matches the long s and dotted capital I; the replacement is
    # identified by the named group, never by re-lowering the matched text.
    assert apply_pronunciations("ſync it", {"sync": "sink"}) == "sink it"
    assert (
        apply_pronunciations("İstanbul is big", {"istanbul": "iss-tan-bool"})
        == "iss-tan-bool is big"
    )


def test_compiled_pattern_is_cached():
    _compiled.cache_clear()
    mapping = {"kubectl": "cube control"}

    apply_pronunciations("kubectl", mapping)
    apply_pronunciations("kubectl", mapping)

    info = _compiled.cache_info()
    assert info.hits >= 1, "the same map must reuse its compiled pattern"


def test_override_wins_and_prepare_text_returns_the_spoken_text(tmp_path: Path):
    store = PronunciationStore(tmp_path / "pronunciations.json")
    store.set("nginx", "engine x")

    assert prepare_text(store, "nginx and nginx", {"NGINX": "en-jin-x"}) == (
        "en-jin-x and en-jin-x"
    )


def test_validate_rejects_oversized_maps_and_terms():
    too_many = {f"w{i}": "x" for i in range(MAX_LEXICON_ENTRIES + 1)}
    with pytest.raises(ValueError, match="limit"):
        validate_pronunciation_map(too_many)

    with pytest.raises(ValueError, match="limit"):
        validate_pronunciation_map({"x" * (MAX_LEXICON_TERM_CHARS + 1): "y"})
    with pytest.raises(ValueError, match="limit"):
        validate_pronunciation_map({"x": "y" * (MAX_LEXICON_TERM_CHARS + 1)})


def test_store_set_enforces_caps_and_allows_replacing_at_the_cap(tmp_path: Path):
    store = PronunciationStore(tmp_path / "pronunciations.json")

    with pytest.raises(ValueError, match="limit"):
        store.set("x" * (MAX_LEXICON_TERM_CHARS + 1), "y")
    with pytest.raises(ValueError, match="limit"):
        store.set("x", "y" * (MAX_LEXICON_TERM_CHARS + 1))

    path = tmp_path / "pronunciations.json"
    path.write_text(
        json.dumps({f"w{i}": "x" for i in range(MAX_LEXICON_ENTRIES)}), "utf-8"
    )
    with pytest.raises(ValueError, match="limit"):
        store.set("new", "y")
    # Updating an existing key does not grow the map, so it remains allowed.
    assert store.set("w0", "z")["w0"] == "z"
