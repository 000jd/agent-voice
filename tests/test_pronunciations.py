"""v0.3.1 pronunciation lexicon: persistence, rewriting, and cache hashing."""

from __future__ import annotations

from pathlib import Path

from agent_voice.pronunciations import (
    PronunciationStore,
    apply_pronunciations,
    prepare_text,
    pronunciation_hash,
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


def test_override_wins_and_prepare_text_returns_the_effective_hash(tmp_path: Path):
    store = PronunciationStore(tmp_path / "pronunciations.json")
    store.set("nginx", "engine x")

    text, digest = prepare_text(store, "nginx and nginx", {"NGINX": "en-jin-x"})

    assert text == "en-jin-x and en-jin-x"
    assert digest == pronunciation_hash({"nginx": "en-jin-x"})


def test_hash_is_deterministic_and_case_insensitive():
    assert pronunciation_hash({"A": "x", "b": "y"}) == pronunciation_hash(
        {"b": "y", "a": "x"}
    )
    assert pronunciation_hash({"a": "x"}) != pronunciation_hash({"a": "z"})
