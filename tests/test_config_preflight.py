"""v0.6.0 pre-flight settings regression."""

from __future__ import annotations

from agent_voice.config import Settings


def test_settings_accepts_field_names() -> None:
    """`extra="ignore"` must not silently drop a constructor field name.

    Before `populate_by_name=True`, `Settings(api_key="x")` returned an empty
    key while the aliased spelling worked — a silent authentication hole for the
    first caller that used the field name.
    """
    assert Settings(api_key="x", _env_file=None).api_key == "x"
