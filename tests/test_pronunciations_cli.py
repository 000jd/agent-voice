"""v0.3.1 CLI: persistent pronunciation lexicon and design candidates."""

from __future__ import annotations

from typer.testing import CliRunner

from agent_voice.cli import app

runner = CliRunner()


def test_pronunciations_cli_set_list_delete():
    result = runner.invoke(app, ["pronunciations", "set", "kubectl", "cube control"])
    assert result.exit_code == 0, result.output
    assert "kubectl" in result.output

    listed = runner.invoke(app, ["pronunciations", "list"])
    assert listed.exit_code == 0
    assert "cube control" in listed.output

    deleted = runner.invoke(app, ["pronunciations", "delete", "KUBECTL"])
    assert deleted.exit_code == 0, deleted.output

    after = runner.invoke(app, ["pronunciations", "list"])
    assert "kubectl" not in after.output

    missing = runner.invoke(app, ["pronunciations", "delete", "missing"])
    assert missing.exit_code == 1


def test_voices_design_exposes_the_candidates_option():
    result = runner.invoke(app, ["voices", "design", "--help"])
    assert result.exit_code == 0
    assert "--candidates" in result.output
