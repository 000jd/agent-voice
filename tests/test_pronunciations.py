"""v0.3.2 pronunciation lexicon: persistence, rewriting, caps and regex cache."""

from __future__ import annotations

import json
import logging
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


# ---------------------------------------------------------------------------
# v0.4.0 M7: the caps are enforced at the store and raw-entry boundaries too
# ---------------------------------------------------------------------------


def test_over_cap_store_file_is_ignored_with_a_warning(tmp_path: Path, caplog):
    """A hand-edited file must never compile an unbounded regex.

    Dropping the whole map is the recoverable answer: the daemon still speaks,
    just without the operator's expansions, and the log says why. Crashing on a
    file an operator can edit by hand would take the daemon down with it.
    """
    path = tmp_path / "pronunciations.json"
    oversized = {f"w{i}": "x" for i in range(MAX_LEXICON_ENTRIES + 1)}
    path.write_text(json.dumps(oversized), "utf-8")
    store = PronunciationStore(path)

    with caplog.at_level(logging.WARNING, logger="agent_voice.pronunciations"):
        assert store.list() == {}
        assert store.effective() == {}
        assert prepare_text(store, "w0 and w1") == "w0 and w1"

    assert any("trimmed" in record.getMessage() for record in caplog.records), caplog.text
    # Writing would replace the operator's over-cap file with one entry, so the
    # mutators refuse instead of quietly destroying the rest of the lexicon.
    with pytest.raises(ValueError, match="trim"):
        store.set("nginx", "engine x")
    with pytest.raises(ValueError, match="trim"):
        store.delete("w0")
    assert len(json.loads(path.read_text("utf-8"))) == MAX_LEXICON_ENTRIES + 1


def test_over_cap_effective_map_is_a_bad_request_with_a_trim_suggestion(tmp_path: Path):
    """The merged map is what reaches the regex, so it is what must pass the caps.

    A valid 400-entry file plus a valid 150-entry per-call map is a 550-alternative
    pattern: each map is within the limits, the merge is not.
    """
    from agent_voice.schemas import AgentVoiceError

    store = PronunciationStore(tmp_path / "pronunciations.json")
    (tmp_path / "pronunciations.json").write_text(
        json.dumps({f"w{i}": "x" for i in range(400)}), "utf-8"
    )
    overrides = {f"o{i}": "y" for i in range(150)}

    with pytest.raises(AgentVoiceError) as excinfo:
        store.effective(overrides)

    error = excinfo.value
    assert error.error_code == "BAD_REQUEST"
    assert "500" in error.message
    assert "delete unused terms" in error.suggestion
    # Both surfaces that speak go through prepare_text, so they inherit the same
    # structured error instead of turning it into an INTERNAL_ERROR.
    with pytest.raises(AgentVoiceError):
        prepare_text(store, "w0", overrides)


def test_over_cap_map_never_reaches_the_regex_compiler():
    """`_compiled` is the expensive, unbounded step: the caps must fire first."""
    from agent_voice.schemas import AgentVoiceError

    _compiled.cache_clear()
    oversized = {f"w{i}": "x" for i in range(MAX_LEXICON_ENTRIES + 1)}

    with pytest.raises(AgentVoiceError) as excinfo:
        apply_pronunciations("w0", oversized)

    assert excinfo.value.error_code == "BAD_REQUEST"
    assert "500" in excinfo.value.message
    assert _compiled.cache_info().misses == 0, "an over-cap map must never compile"


async def test_openai_surface_rejects_a_raw_over_cap_map():
    """routes_openai takes free-form maps too, so it validates before use."""
    from types import SimpleNamespace

    from agent_voice.api.routes_openai import run_synthesis
    from agent_voice.schemas import AgentVoiceError

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                ctx=SimpleNamespace(settings=SimpleNamespace(max_text_chars=500))
            )
        )
    )

    with pytest.raises(AgentVoiceError) as excinfo:
        await run_synthesis(
            request,
            text="hello",
            voice=None,
            engine=None,
            model=None,
            quality="high",
            language="Auto",
            response_format="wav",
            seed=None,
            speed=1.0,
            pronunciations={f"w{i}": "x" for i in range(MAX_LEXICON_ENTRIES + 1)},
        )

    assert excinfo.value.error_code == "BAD_REQUEST"
    assert "500" in excinfo.value.message
    assert excinfo.value.suggestion
