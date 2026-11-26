"""v0.4.0 scoped pronunciation lexicon, code normalization and the CLI on top.

Three surfaces meet here and none may drift:

* :class:`~agent_voice.pronunciations.ScopedLexicon` layers global -> project ->
  voice -> per-call. Every scope is capped at 500 entries, the merge at 1,500, and
  the caps fire before ``_compiled`` builds a regex. Project names and voice ids
  become filenames, so they are validated rather than interpolated.
* :func:`~agent_voice.text_norm.normalize_text` applies the lexicon first and
  expands code-shaped identifiers only in the spans the lexicon did *not* rewrite.
  Subtitles keep the written text; only the audio speaks the rewrite.
* ``agent-voice pronunciations list|set|delete|show|check`` is the daemon-free
  operator surface for the same files.

Everything here is CPU-only: renders use the ``mock`` engine, and the scoped
files live under ``tmp_path``. The two full renders assert real pipeline
behaviour (cache isolation and subtitle text); the rest drive the primitives
directly so a failure names the layer that broke.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agent_voice.api.app import create_app
from agent_voice.cache import CACHE_KEY_FIELDS
from agent_voice.cli import app as cli_app
from agent_voice.config import Settings, get_settings
from agent_voice.pronunciations import (
    MAX_LEXICON_ENTRIES,
    MAX_MERGED_LEXICON_ENTRIES,
    ScopedLexicon,
    _compiled,
    apply_pronunciations_spans,
    lexicon_hash,
)
from agent_voice.schemas import AgentVoiceError, ErrorCode
from agent_voice.text_norm import normalize_text

REPO_ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _lexicon(tmp_path: Path) -> ScopedLexicon:
    """A scoped lexicon over the same layout Settings uses: data/ + data/voices/."""
    return ScopedLexicon(tmp_path / "data", tmp_path / "data" / "voices")


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")


def _make_voice(voices_dir: Path, voice_id: str, *, metadata: bool = True) -> Path:
    """A voice directory, optionally without the metadata.json that makes it real.

    ``ScopedLexicon.path_for_voice`` uses exactly the one existence rule
    ``VoiceStore.resolve`` uses — ``<voices>/<id>/metadata.json`` is a file — so a
    directory plus that file is all a voice lexicon needs to be reachable.
    """
    voice_dir = voices_dir / voice_id
    voice_dir.mkdir(parents=True, exist_ok=True)
    if metadata:
        _write_json(
            voice_dir / "metadata.json",
            {
                "voice_id": voice_id,
                "name": f"Voice {voice_id}",
                "ref_wav_sha256": "0" * 64,
                "ref_text": "",
                "duration_sec": 3.0,
                "created_at": 0.0,
            },
        )
    return voice_dir


def _settings(tmp_path: Path) -> Settings:
    """The same isolated Settings the HTTP test suites build, pointed at tmp_path."""
    return Settings(
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=REPO_ROOT / "engines",
        AGENT_VOICE_DEFAULT_ENGINE="mock",
        MCP_BASE_PATH="",
        AGENT_VOICE_ALLOWED_HOSTS="testserver",
    )


@pytest.fixture
def scoped_client(tmp_path: Path) -> Iterator[TestClient]:
    """A real app (MCP mount off) with the mock engine and no network."""
    app = create_app(settings=_settings(tmp_path), mount_mcp=False)
    with TestClient(app) as client:
        yield client


def _speak(client: TestClient, payload: dict[str, object]) -> dict:
    response = client.post("/v1/agent/speak", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _text(result) -> str:
    """Combined output: Click versions disagree on whether stderr is folded in."""
    combined = getattr(result, "output", "")
    stderr = getattr(result, "stderr", "")
    return combined if stderr in combined else combined + stderr


def _run(*args: str):
    return runner.invoke(cli_app, list(args))


def _payload(result) -> dict:
    output = _text(result)
    assert result.exit_code == 0, output
    return json.loads(output)


def _assert_exit(result, code: int) -> str:
    """Assert the command exited on purpose, never by leaking a traceback."""
    output = _text(result)
    assert result.exit_code == code, output
    assert isinstance(result.exception, SystemExit) or result.exception is None, (
        f"expected typer.Exit, raised {result.exception!r}"
    )
    assert "Traceback (most recent call last)" not in output, output
    return output


# ---------------------------------------------------------------------------
# Precedence: global -> project -> voice -> per-call
# ---------------------------------------------------------------------------


def test_each_scope_layer_wins_over_the_one_below_it(tmp_path: Path):
    """The merge is additive and ordered; later layers win, not replace."""
    lexicon = _lexicon(tmp_path)
    lexicon.store_for().set("Term", "global")
    assert lexicon.effective() == {"term": "global"}

    lexicon.store_for(project="alpha").set("term", "project")
    assert lexicon.effective("alpha") == {"term": "project"}
    # A project without an entry of its own still sees the global layer.
    assert lexicon.effective("beta") == {"term": "global"}

    _make_voice(tmp_path / "data" / "voices", "voice123")
    lexicon.store_for(voice_id="voice123").set("term", "voice")
    assert lexicon.effective(None, "voice123") == {"term": "voice"}
    assert lexicon.effective("alpha", "voice123") == {"term": "voice"}

    overrides = {"TERM": "call"}
    assert lexicon.effective("alpha", "voice123", overrides) == {"term": "call"}
    # Keys are lower-cased at every layer, which is why TERM collided at all.
    assert lexicon.effective("alpha", voice_id="voice123") == {"term": "voice"}


def test_per_call_overrides_mutate_neither_the_stores_nor_the_scope_cache(tmp_path: Path):
    """A per-call map is for one request: it must not leak into disk or the merge."""
    lexicon = _lexicon(tmp_path)
    lexicon.store_for().set("kubectl", "cube control")
    lexicon.store_for(project="alpha").set("kubectl", "project control")
    before = lexicon.effective("alpha")

    assert lexicon.effective("alpha", overrides={"kubectl": "call control"}) == {
        "kubectl": "call control"
    }

    # The merge cache was not rewritten by the override.
    assert lexicon.effective("alpha") == before == {"kubectl": "project control"}
    # Neither file was touched.
    assert lexicon.store_for().list() == {"kubectl": "cube control"}
    assert lexicon.store_for(project="alpha").list() == {"kubectl": "project control"}
    assert json.loads(
        (tmp_path / "data" / "pronunciations" / "alpha.json").read_text("utf-8")
    ) == {"kubectl": "project control"}
    # Repeating the same per-call merge is idempotent, not accumulating.
    assert lexicon.effective("alpha", overrides={"kubectl": "call control"}) == {
        "kubectl": "call control"
    }
    # effective() hands out a fresh copy: a caller may annotate it freely.
    handed_out = lexicon.effective("alpha")
    handed_out["injected"] = "nope"
    assert "injected" not in lexicon.effective("alpha")


def test_nfd_scope_keys_and_values_are_normalised_to_nfc(tmp_path: Path):
    """A hand-edited file may be decomposed; matching must still be by NFC form."""
    lexicon = _lexicon(tmp_path)
    nfc_key = "café"
    nfd_key = unicodedata.normalize("NFD", nfc_key)
    nfd_value = unicodedata.normalize("NFD", "caffè")
    assert nfd_key != nfc_key, "the test needs a genuinely decomposed key"

    _write_json(tmp_path / "data" / "pronunciations.json", {nfd_key: nfd_value})
    # No project/voice selector: this is the global layer only.
    effective = lexicon.effective()
    assert effective == {nfc_key: unicodedata.normalize("NFC", nfd_value)}
    assert all(unicodedata.is_normalized("NFC", key) for key in effective)
    assert all(unicodedata.is_normalized("NFC", value) for value in effective.values())

    # The decomposed key on disk matches NFC text written by the caller.
    rewritten, _spans = apply_pronunciations_spans(
        unicodedata.normalize("NFC", "A café here"), effective
    )
    assert rewritten == "A caffè here"

    # Per-call keys get the same treatment, so a decomposed override collides
    # with the NFC key instead of becoming a second entry.
    per_call = lexicon.effective(overrides={nfd_key: "per call"})
    assert per_call == {nfc_key: "per call"}
    assert unicodedata.is_normalized("NFC", next(iter(per_call)))


def test_unicode_case_folding_matches_through_the_named_group(tmp_path: Path):
    """``ſync``/``İstanbul`` fold to the key; ``match.lastgroup`` picks the value.

    A ``match.group(0).lower()`` lookup would miss both and raise ``KeyError``;
    the generated named groups are what make the fold safe.
    """
    lexicon = _lexicon(tmp_path)
    lexicon.store_for().set("sync", "sink")
    lexicon.store_for(project="alpha").set("istanbul", "iss tan bool")

    rewritten, _spans = apply_pronunciations_spans(
        "ſync İstanbul", lexicon.effective("alpha")
    )
    assert rewritten == "sink iss tan bool"


def test_regex_metacharacters_in_keys_are_matched_literally(tmp_path: Path):
    """Keys are ``re.escape``d into alternatives, never compiled as patterns."""
    lexicon = _lexicon(tmp_path)
    lexicon.store_for().set("c++", "see plus plus")
    lexicon.store_for().set("a|b", "a or b")
    lexicon.store_for().set("(x)", "open x close")

    rewritten, _spans = apply_pronunciations_spans(
        "c++ and a|b and (x)", lexicon.effective()
    )
    assert rewritten == "see plus plus and a or b and open x close"


def test_scope_paths_lists_the_merge_inputs_in_precedence_order(tmp_path: Path):
    lexicon = _lexicon(tmp_path)
    _make_voice(tmp_path / "data" / "voices", "voice123")
    global_path = tmp_path / "data" / "pronunciations.json"
    project_path = tmp_path / "data" / "pronunciations" / "alpha.json"
    voice_path = (
        tmp_path / "data" / "voices" / "voice123" / "pronunciations.json"
    )

    assert lexicon.scope_paths() == [global_path]
    assert lexicon.scope_paths("alpha") == [global_path, project_path]
    assert lexicon.scope_paths("alpha", "voice123") == [
        global_path,
        project_path,
        voice_path,
    ]
    # A voice that does not exist contributes no path at all.
    assert lexicon.scope_paths(None, "ghost") == [global_path]


# ---------------------------------------------------------------------------
# Path safety: slugs, voice ids and the request string
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slug",
    [
        "../x",
        "../../etc/passwd",
        "Infra",
        "IN",
        "a",
        "_infra",
        "in fra",
        "x/y",
        "a" * 42,
    ],
)
def test_project_names_are_validated_slugs(tmp_path: Path, slug: str):
    """The project name becomes a filename, so traversal and casing are rejected."""
    lexicon = _lexicon(tmp_path)
    with pytest.raises(ValueError, match="must match"):
        lexicon.path_for_project(slug)
    with pytest.raises(AgentVoiceError) as excinfo:
        lexicon.effective(slug)
    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    assert "invalid pronunciation project" in excinfo.value.message
    with pytest.raises(ValueError, match="must match"):
        lexicon.store_for(project=slug)


def test_the_longest_project_slug_is_accepted(tmp_path: Path):
    """2-41 characters is the contract; 41 is the boundary, 42 is the parametrized reject."""
    lexicon = _lexicon(tmp_path)
    slug = "a" * 41
    assert lexicon.path_for_project(slug) == (
        tmp_path / "data" / "pronunciations" / f"{slug}.json"
    )


def test_voice_scope_needs_a_real_voice_directory(tmp_path: Path):
    lexicon = _lexicon(tmp_path)
    voices = tmp_path / "data" / "voices"

    # No directory at all.
    assert lexicon.path_for_voice("abc123") is None
    with pytest.raises(ValueError, match="no such voice"):
        lexicon.store_for(voice_id="abc123")

    # A directory without metadata.json is not a registered voice, even if it
    # already carries a lexicon file.
    voice_dir = _make_voice(voices, "abc123", metadata=False)
    _write_json(voice_dir / "pronunciations.json", {"ghost": "boo"})
    assert lexicon.path_for_voice("abc123") is None
    assert lexicon.effective(None, "abc123") == {}

    # metadata.json is what makes the voice scope exist: the file that was inert
    # a moment ago now contributes, and the lexicon file itself stays optional.
    _make_voice(voices, "abc123")
    assert lexicon.path_for_voice("abc123") == voice_dir / "pronunciations.json"
    assert lexicon.effective(None, "abc123") == {"ghost": "boo"}

    _make_voice(voices, "voice2")
    assert lexicon.path_for_voice("voice2") == voices / "voice2" / "pronunciations.json"
    assert lexicon.effective(None, "voice2") == {}


@pytest.mark.parametrize(
    "voice_id",
    [
        "../x",
        "..",
        ".",
        "a/b",
        "a\\b",
        ".hidden",
        "a" * 65,
        "a..b",
        "%2e%2e%2fx",
        "a b",
        "-dash",
        "",
    ],
)
def test_voice_ids_are_directory_names_never_paths(tmp_path: Path, voice_id: str):
    """No request string is interpolated into a path: traversal yields no scope."""
    lexicon = _lexicon(tmp_path)
    assert lexicon.path_for_voice(voice_id) is None
    with pytest.raises(ValueError, match="no such voice"):
        lexicon.store_for(voice_id=voice_id)


def test_a_symlinked_voice_directory_cannot_escape_the_voices_root(tmp_path: Path):
    """The resolved parent must be the voices dir itself, not wherever a link points."""
    lexicon = _lexicon(tmp_path)
    voices = tmp_path / "data" / "voices"
    voices.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_json(outside / "metadata.json", {"voice_id": "linked"})
    link = voices / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:  # pragma: no cover - platform without symlink support
        pytest.skip("symlinks are unavailable on this filesystem")

    assert lexicon.path_for_voice("linked") is None
    with pytest.raises(ValueError, match="no such voice"):
        lexicon.store_for(voice_id="linked")


def test_store_for_refuses_to_guess_between_two_scopes(tmp_path: Path):
    lexicon = _lexicon(tmp_path)
    _make_voice(tmp_path / "data" / "voices", "voice123")

    with pytest.raises(ValueError, match="one scope"):
        lexicon.store_for(project="alpha", voice_id="voice123")

    # Unscoped still resolves to the global file, which is the same file the
    # global PronunciationStore owns.
    assert lexicon.store_for().path == tmp_path / "data" / "pronunciations.json"


def test_the_request_project_string_is_validated_before_any_path(
    scoped_client: TestClient, tmp_path: Path
):
    """A traversal attempt in a request body is a 422, never a filesystem lookup."""
    response = scoped_client.post(
        "/v1/agent/speak", json={"text": "hello", "project": "../x"}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "BAD_REQUEST"
    assert "project" in body["message"]
    # Nothing was written inside the lexicon directory and nothing escaped it.
    project_dir = tmp_path / "data" / "pronunciations"
    assert list(project_dir.iterdir()) == []
    assert not (tmp_path / "x.json").exists()


# ---------------------------------------------------------------------------
# Caps: 500 per scope, 1,500 merged, and the regex compiler behind them
# ---------------------------------------------------------------------------


def test_a_scope_at_the_500_entry_cap_still_applies(tmp_path: Path):
    lexicon = _lexicon(tmp_path)
    _write_json(
        tmp_path / "data" / "pronunciations" / "alpha.json",
        {f"w{i}": f"x{i}" for i in range(MAX_LEXICON_ENTRIES)},
    )
    assert len(lexicon.effective("alpha")) == MAX_LEXICON_ENTRIES


def test_an_over_cap_scope_file_is_ignored_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """One bad file must not silence the other scopes or crash the request."""
    lexicon = _lexicon(tmp_path)
    lexicon.store_for().set("kubectl", "cube control")
    over_cap = tmp_path / "data" / "pronunciations" / "alpha.json"
    _write_json(over_cap, {f"w{i}": "x" for i in range(MAX_LEXICON_ENTRIES + 1)})

    with caplog.at_level(logging.WARNING, logger="agent_voice.pronunciations"):
        merged = lexicon.effective("alpha")

    assert merged == {"kubectl": "cube control"}
    assert any("trimmed" in record.getMessage() for record in caplog.records), (
        caplog.text
    )
    assert any(str(over_cap) in record.getMessage() for record in caplog.records)

    # The scoped write path is the same store as the global one: it refuses to
    # replace an operator file the caps rejected.
    with pytest.raises(ValueError, match="trim"):
        lexicon.store_for(project="alpha").set("nginx", "engine x")
    assert len(json.loads(over_cap.read_text("utf-8"))) == MAX_LEXICON_ENTRIES + 1


def test_the_merged_cap_allows_1500_entries(tmp_path: Path):
    """500 + 500 + 500 is the largest merge that may reach the regex."""
    lexicon = _lexicon(tmp_path)
    _write_json(
        tmp_path / "data" / "pronunciations.json",
        {f"g{i}": "x" for i in range(MAX_LEXICON_ENTRIES)},
    )
    _write_json(
        tmp_path / "data" / "pronunciations" / "alpha.json",
        {f"p{i}": "x" for i in range(MAX_LEXICON_ENTRIES)},
    )
    _make_voice(tmp_path / "data" / "voices", "voice123")
    _write_json(
        tmp_path / "data" / "voices" / "voice123" / "pronunciations.json",
        {f"v{i}": "x" for i in range(MAX_LEXICON_ENTRIES)},
    )

    effective = lexicon.effective("alpha", "voice123")
    assert len(effective) == MAX_MERGED_LEXICON_ENTRIES == 1500


def test_an_over_cap_merge_is_a_structured_bad_request(tmp_path: Path):
    """Valid scopes whose merge is over the cap are the caller's 400, with a way out."""
    lexicon = _lexicon(tmp_path)
    _write_json(
        tmp_path / "data" / "pronunciations.json",
        {f"g{i}": "x" for i in range(MAX_LEXICON_ENTRIES)},
    )
    _write_json(
        tmp_path / "data" / "pronunciations" / "alpha.json",
        {f"p{i}": "x" for i in range(MAX_LEXICON_ENTRIES)},
    )
    _make_voice(tmp_path / "data" / "voices", "voice123")
    _write_json(
        tmp_path / "data" / "voices" / "voice123" / "pronunciations.json",
        {f"v{i}": "x" for i in range(MAX_LEXICON_ENTRIES)},
    )

    with pytest.raises(AgentVoiceError) as excinfo:
        lexicon.effective("alpha", "voice123", {"one_more": "x"})

    error = excinfo.value
    assert error.error_code == ErrorCode.BAD_REQUEST
    assert "1501" in error.message
    assert "1500" in error.message
    assert "1500 merged" in error.suggestion
    assert "delete unused terms" in error.suggestion


def test_a_per_call_map_obeys_the_per_scope_cap_on_its_own(tmp_path: Path):
    lexicon = _lexicon(tmp_path)
    over_cap = {f"o{i}": "x" for i in range(MAX_LEXICON_ENTRIES + 1)}
    with pytest.raises(AgentVoiceError) as excinfo:
        lexicon.effective(overrides=over_cap)
    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    assert "overrides" in excinfo.value.message


def test_the_compiled_regex_cache_never_sees_an_over_cap_map():
    """``_compiled`` is the unbounded step; the caps must fire before it."""
    _compiled.cache_clear()
    try:
        over_cap = {
            f"k{i}": "x" for i in range(MAX_MERGED_LEXICON_ENTRIES + 1)
        }
        with pytest.raises(AgentVoiceError) as excinfo:
            apply_pronunciations_spans("k0", over_cap)
        assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
        assert _compiled.cache_info().misses == 0

        # The boundary itself compiles once and is then reused.
        at_cap = {f"k{i}": "x" for i in range(MAX_MERGED_LEXICON_ENTRIES)}
        assert apply_pronunciations_spans("k0", at_cap)[0] == "x"
        assert apply_pronunciations_spans("k0", at_cap)[0] == "x"
        info = _compiled.cache_info()
        assert info.currsize == 1
        assert info.hits >= 1
    finally:
        _compiled.cache_clear()


def test_the_lexicon_hash_is_order_insensitive_and_changes_on_edit(tmp_path: Path):
    """``hash`` is observability: same map, same digest; edited term, new digest."""
    assert lexicon_hash({"a": "1", "b": "2"}) == lexicon_hash({"b": "2", "a": "1"})

    lexicon = _lexicon(tmp_path)
    lexicon.store_for().set("alpha", "one")
    lexicon.store_for().set("beta", "two")
    first = lexicon.hash()
    assert first == lexicon.hash()
    # Dict insertion order (and the casing of an override key) cannot change it.
    assert lexicon.hash(overrides={"A": "1", "b": "2"}) == lexicon.hash(
        overrides={"b": "2", "A": "1"}
    )

    lexicon.store_for().set("alpha", "une")
    assert lexicon.hash() != first


def test_pronunciation_hash_and_project_are_not_cache_key_fields():
    """The spoken text is the discriminator; the lexicon itself is never keyed."""
    assert "pronunciation_hash" not in CACHE_KEY_FIELDS
    assert "project" not in CACHE_KEY_FIELDS
    assert "text" in CACHE_KEY_FIELDS
    assert "speed" in CACHE_KEY_FIELDS


# ---------------------------------------------------------------------------
# Scoped cache isolation: one scope's edit invalidates only its own lines
# ---------------------------------------------------------------------------


def test_editing_one_project_lexicon_leaves_another_projects_line_cached(
    scoped_client: TestClient, tmp_path: Path
):
    """A synthesis cache is keyed on the spoken text, so only edited lines re-render."""
    lexicon = _lexicon(tmp_path)
    lexicon.store_for(project="alpha").set("alpha_term", "alpha spoken")
    lexicon.store_for(project="beta").set("beta_term", "beta spoken")
    alpha_request = {"text": "alpha_term appears here.", "project": "alpha"}
    beta_request = {"text": "beta_term appears here.", "project": "beta"}

    first_alpha = _speak(scoped_client, alpha_request)
    second_alpha = _speak(scoped_client, alpha_request)
    first_beta = _speak(scoped_client, beta_request)
    second_beta = _speak(scoped_client, beta_request)

    assert first_alpha["cached"] is False
    assert second_alpha["cached"] is True
    assert first_beta["cached"] is False
    assert second_beta["cached"] is True

    # A project edit through the same store the CLI uses.
    lexicon.store_for(project="alpha").set("alpha_term", "alpha changed")

    changed_alpha = _speak(scoped_client, alpha_request)
    untouched_beta = _speak(scoped_client, beta_request)

    assert changed_alpha["cached"] is False, "the edited project's line must re-render"
    assert changed_alpha["artifact_id"] != second_alpha["artifact_id"]
    assert untouched_beta["cached"] is True, "beta's line never changed"
    assert untouched_beta["artifact_id"] == second_beta["artifact_id"]


def test_editing_one_voice_lexicon_leaves_another_voices_merge_intact(tmp_path: Path):
    """The merge memo is per (project, voice); either side can be edited alone.

    A direct ``effective`` assertion rather than a render: the voice layer is the
    same merge path the project test exercises end to end, and this keeps the
    suite off a second pair of worker spawns.
    """
    lexicon = _lexicon(tmp_path)
    _make_voice(tmp_path / "data" / "voices", "voice1")
    _make_voice(tmp_path / "data" / "voices", "voice2")
    lexicon.store_for(voice_id="voice1").set("term", "one")
    lexicon.store_for(voice_id="voice2").set("term", "two")

    voice2_before = lexicon.effective(None, "voice2")
    assert voice2_before == {"term": "two"}

    lexicon.store_for(voice_id="voice1").set("term", "one changed")

    assert lexicon.effective(None, "voice1") == {"term": "one changed"}
    assert lexicon.effective(None, "voice2") == voice2_before
    assert lexicon.store_for(voice_id="voice2").list() == {"term": "two"}


def test_the_merged_cap_is_a_structured_bad_request_on_the_rest_surface(
    scoped_client: TestClient, tmp_path: Path
):
    """The route must answer with the canonical envelope, never INTERNAL_ERROR."""
    data = tmp_path / "data"
    _write_json(
        data / "pronunciations.json",
        {f"g{i}": "x" for i in range(MAX_LEXICON_ENTRIES)},
    )
    _write_json(
        data / "pronunciations" / "alpha.json",
        {f"p{i}": "x" for i in range(MAX_LEXICON_ENTRIES)},
    )
    _make_voice(data / "voices", "voice123")
    _write_json(
        data / "voices" / "voice123" / "pronunciations.json",
        {f"v{i}": "x" for i in range(MAX_LEXICON_ENTRIES)},
    )

    response = scoped_client.post(
        "/v1/agent/speak",
        json={
            "text": "hello",
            "project": "alpha",
            "voice": "voice123",
            "pronunciations": {"one_more": "x"},
        },
    )

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error_code"] == "BAD_REQUEST"
    assert "1500" in body["message"]
    assert body["suggestion"], "an over-cap error must say how to fix it"


# ---------------------------------------------------------------------------
# CLI: pronunciations list|set|delete|show|check with --project/--voice
# ---------------------------------------------------------------------------


def test_cli_project_scope_roundtrip():
    settings = get_settings()
    project_file = settings.pronunciations_dir / "infra.json"

    created = _run(
        "pronunciations", "set", "kubectl", "cube control", "--project", "infra"
    )
    assert _payload(created) == {"kubectl": "cube control"}
    assert json.loads(project_file.read_text("utf-8")) == {"kubectl": "cube control"}

    _payload(
        _run("pronunciations", "set", "k8s", "K eight S", "--project", "infra")
    )
    listed = _payload(_run("pronunciations", "list", "--project", "infra"))
    assert listed == {"k8s": "K eight S", "kubectl": "cube control"}

    deleted = _payload(
        _run("pronunciations", "delete", "kubectl", "--project", "infra")
    )
    assert deleted == {"deleted": "kubectl"}
    assert _payload(_run("pronunciations", "list", "--project", "infra")) == {
        "k8s": "K eight S"
    }

    # The global map was never touched by a scoped write.
    assert _payload(_run("pronunciations", "list")) == {}

    # Deleting a missing term is a real failure, not a silent success.
    missing = _run("pronunciations", "delete", "kubectl", "--project", "infra")
    output = _assert_exit(missing, 1)
    assert "no such pronunciation" in output


def test_cli_voice_scope_roundtrip():
    settings = get_settings()
    _make_voice(settings.voices_dir, "voice1")
    voice_file = settings.voices_dir / "voice1" / "pronunciations.json"

    created = _run(
        "pronunciations", "set", "nginx", "engine x", "--voice", "voice1"
    )
    assert _payload(created) == {"nginx": "engine x"}
    assert json.loads(voice_file.read_text("utf-8")) == {"nginx": "engine x"}
    assert _payload(_run("pronunciations", "list", "--voice", "voice1")) == {
        "nginx": "engine x"
    }

    shown = _payload(_run("pronunciations", "show", "--voice", "voice1"))
    assert shown["project"] is None
    assert shown["voice_id"] == "voice1"
    assert [scope["scope"] for scope in shown["scopes"]] == ["global", "voice:voice1"]
    assert shown["effective"] == {"nginx": "engine x"}

    assert _payload(
        _run("pronunciations", "delete", "nginx", "--voice", "voice1")
    ) == {"deleted": "nginx"}
    assert json.loads(voice_file.read_text("utf-8")) == {}
    assert _payload(_run("pronunciations", "list", "--voice", "voice1")) == {}

    unknown = _run("pronunciations", "list", "--voice", "missing")
    assert "no such voice" in _assert_exit(unknown, 1)


def test_cli_show_prints_every_layer_the_merged_map_and_the_hash():
    settings = get_settings()
    _make_voice(settings.voices_dir, "voice1")

    _payload(_run("pronunciations", "set", "kubectl", "cube control"))
    _payload(
        _run("pronunciations", "set", "k8s", "K eight S", "--project", "infra")
    )
    _payload(
        _run("pronunciations", "set", "nginx", "engine x", "--voice", "voice1")
    )

    shown = _payload(
        _run(
            "pronunciations",
            "show",
            "--project",
            "infra",
            "--voice",
            "voice1",
        )
    )
    assert shown["project"] == "infra"
    assert shown["voice_id"] == "voice1"
    assert [scope["scope"] for scope in shown["scopes"]] == [
        "global",
        "project:infra",
        "voice:voice1",
    ]
    assert shown["effective"] == {
        "k8s": "K eight S",
        "kubectl": "cube control",
        "nginx": "engine x",
    }
    assert shown["merged_entries"] == 3
    assert len(shown["pronunciation_hash"]) == 64
    int(shown["pronunciation_hash"], 16)


def test_cli_check_flags_unknown_all_caps_camel_snake_and_versions():
    _payload(_run("pronunciations", "set", "kubectl", "cube control", "--project", "infra"))

    checked = _payload(
        _run(
            "pronunciations",
            "check",
            "kubectl HTTPServer worker_manager v0.4.0 GPU",
            "--project",
            "infra",
            "--spell-identifiers",
        )
    )

    assert checked["source"] == "(argument)"
    assert checked["project"] == "infra"
    assert checked["rewritten"] == [
        {"term": "kubectl", "spoken": "cube control", "count": 1}
    ]
    assert checked["unknown_acronyms"] == ["GPU"]

    identifiers = {entry["term"]: entry for entry in checked["unknown_identifiers"]}
    assert {"HTTPServer", "worker_manager", "v0.4.0"} <= set(identifiers)
    assert identifiers["HTTPServer"]["code_mode"] == "HTTP Server"
    assert identifiers["HTTPServer"]["spelled"] == "H T T P S E R V E R"
    assert identifiers["worker_manager"]["code_mode"] == "worker manager"
    assert identifiers["v0.4.0"]["code_mode"] == "version zero point four dot zero"
    # The lexicon-covered term is reported as a rewrite, not as an unknown.
    assert "kubectl" not in identifiers


def test_cli_check_uses_the_effective_lexicon_of_all_selected_scopes():
    settings = get_settings()
    _make_voice(settings.voices_dir, "voice1")
    _payload(_run("pronunciations", "set", "GPU", "gee pee you"))
    _payload(
        _run("pronunciations", "set", "GPU", "project wins", "--project", "infra")
    )
    _payload(
        _run("pronunciations", "set", "GPU", "gee pee you voice", "--voice", "voice1")
    )

    # A covered acronym disappears from unknown_acronyms only when the *merged*
    # map covers it; the voice layer wins over the project layer.
    checked = _payload(
        _run(
            "pronunciations",
            "check",
            "GPU",
            "--project",
            "infra",
            "--voice",
            "voice1",
            "--spell-identifiers",
        )
    )
    assert checked["rewritten"] == [
        {"term": "GPU", "spoken": "gee pee you voice", "count": 1}
    ]
    assert checked["unknown_acronyms"] == []
    assert checked["unknown_identifiers"] == []

    project_only = _payload(
        _run("pronunciations", "check", "GPU", "--project", "infra")
    )
    assert project_only["rewritten"][0]["spoken"] == "project wins"


def test_cli_check_reads_a_file_and_reports_its_path(tmp_path: Path):
    _payload(_run("pronunciations", "set", "kubectl", "cube control", "--project", "infra"))
    snippet = tmp_path / "snippet.txt"
    snippet.write_text("kubectl and worker_manager\n", "utf-8")

    checked = _payload(
        _run(
            "pronunciations",
            "check",
            str(snippet),
            "--project",
            "infra",
            "--spell-identifiers",
        )
    )

    assert checked["source"] == str(snippet)
    assert checked["characters"] == len("kubectl and worker_manager\n")
    assert checked["rewritten"][0]["term"] == "kubectl"
    terms = {entry["term"] for entry in checked["unknown_identifiers"]}
    assert "worker_manager" in terms


def test_cli_check_omits_identifier_expansion_without_the_flag():
    checked = _payload(
        _run("pronunciations", "check", "HTTPServer and GPU")
    )
    assert "unknown_identifiers" not in checked
    assert checked["unknown_acronyms"] == ["GPU"]


@pytest.mark.parametrize(
    "command",
    [
        ("list",),
        ("show",),
        ("check", "hello"),
        ("set", "word", "replacement"),
        ("delete", "word"),
    ],
)
def test_cli_bad_project_slug_fails_cleanly(command: tuple[str, ...]):
    """Operator input gets one red line and exit 1 — never a traceback."""
    result = _run("pronunciations", *command, "--project", "../x")
    output = _assert_exit(result, 1)
    assert "invalid" in output.lower()
    assert "must match" in output
    # Nothing was resolved into a path, so no lexicon directory was written.
    settings = get_settings()
    assert not settings.pronunciations_dir.exists()


@pytest.mark.parametrize(
    "command",
    [("list",), ("set", "word", "replacement"), ("delete", "word")],
)
def test_cli_scope_commands_reject_two_scopes_at_once(command: tuple[str, ...]):
    settings = get_settings()
    _make_voice(settings.voices_dir, "voice1")
    result = _run(
        "pronunciations",
        *command,
        "--project",
        "infra",
        "--voice",
        "voice1",
    )
    output = _assert_exit(result, 1)
    assert "pick one scope" in output
    assert not settings.pronunciations_dir.exists()


@pytest.mark.parametrize("flag", ["--project", "--voice"])
def test_cli_empty_scope_is_rejected_not_treated_as_global(flag: str):
    """An explicitly empty scope is operator input; it must never write global."""
    settings = get_settings()
    result = _run(
        "pronunciations",
        "set",
        "empty",
        "scope",
        flag,
        "",
    )
    _assert_exit(result, 1)
    assert not settings.pronunciations_path.exists()
    assert not settings.pronunciations_dir.exists()


# ---------------------------------------------------------------------------
# normalize_text: lexicon first, code normalization outside its spans
# ---------------------------------------------------------------------------


def test_normalize_text_protects_lexicon_replacements_and_expands_only_the_rest(
    tmp_path: Path,
):
    """A replacement may itself look like code; it is output, never input again."""
    lexicon = _lexicon(tmp_path)
    lexicon.store_for(project="alpha").set("kubectl", "kube_control")
    _make_voice(tmp_path / "data" / "voices", "voice1")
    lexicon.store_for(voice_id="voice1").set("nginx", "engine_x")
    mapping = lexicon.effective("alpha", "voice1", {"gpu": "gee pee you"})

    written = "Run kubectl and nginx on GPU with worker_manager at src/app.py."

    without_code_mode = normalize_text(written, mapping=mapping)
    assert without_code_mode == (
        "Run kube_control and engine_x on gee pee you with worker_manager at "
        "src/app.py."
    )

    spoken = normalize_text(written, mapping=mapping, expand_identifiers=True)
    # kube_control / engine_x / gee pee you are protected spans: had they been
    # handed to code mode they would have become "kube control", "engine x" and
    # "gee pee you" is already words. The surrounding identifier and path are
    # expanded exactly once.
    assert spoken == (
        "Run kube_control and engine_x on gee pee you with worker manager at "
        "src slash app dot py."
    )
    assert "kube control" not in spoken
    assert "engine x" not in spoken


def test_normalize_text_code_expansion_is_idempotent():
    """Expanding an already-spoken line changes nothing, so no double expansion."""
    written = "worker_manager HTTPServer v0.4.0"
    once = normalize_text(written, mapping={}, expand_identifiers=True)
    assert once == "worker manager HTTP Server version zero point four dot zero"
    assert normalize_text(once, mapping={}, expand_identifiers=True) == once


def test_a_script_render_keeps_the_written_text_in_its_subtitles(
    scoped_client: TestClient, tmp_path: Path
):
    """The audio says the rewrite; .srt/.vtt/.timing.json show what was scripted."""
    lexicon = _lexicon(tmp_path)
    lexicon.store_for(project="alpha").set("kubectl", "cube control")
    written = "Run kubectl get pods now."
    # The spoken form really is the rewritten one — this is what the engine hears.
    assert normalize_text(
        written, mapping=lexicon.effective("alpha"), expand_identifiers=True
    ) == "Run cube control get pods now."

    response = scoped_client.post(
        "/v1/agent/script",
        json={
            "beats": [{"text": written}],
            "project": "alpha",
            "export_srt": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["beats"][0]["text"] == written

    srt = Path(body["srt_path"]).read_text("utf-8")
    assert written in srt
    assert "cube control" not in srt

    vtt = Path(body["vtt_path"]).read_text("utf-8")
    assert written in vtt
    assert "cube control" not in vtt

    timing = json.loads(Path(body["timing_json_path"]).read_text("utf-8"))
    assert timing["beats"][0]["text"] == written
