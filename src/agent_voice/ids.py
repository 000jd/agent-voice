"""Resource-id validation before any filesystem join.

Route parameters and settings values are not paths. They are checked here first,
so a 300-char value can never reach the OS as a filename and turn into a 500 that
leaks the data directory.
"""

from __future__ import annotations

import re

from .schemas import AgentVoiceError, ErrorCode

MAX_RESOURCE_ID_CHARS = 64
RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")


def _bad(value: str, noun: str, hint: str) -> AgentVoiceError:
    return AgentVoiceError(
        ErrorCode.BAD_REQUEST,
        f"invalid {noun}: {value[:32]}{'...' if len(value) > 32 else ''}",
        hint,
    )


def validate_resource_id(
    value: str,
    *,
    noun: str = "id",
    hint: str = "Use an id returned by the API.",
) -> str:
    """Validate a human-facing voice name or generated resource id.

    The generated ids are 12/16 hex, but voice names are accepted by the
    name-resolution paths and must stay valid without becoming path syntax.
    """
    if not isinstance(value, str):
        raise _bad(str(value), noun, hint)
    if not value or len(value) > MAX_RESOURCE_ID_CHARS:
        raise _bad(value, noun, hint)
    if ".." in value or "/" in value or "\\" in value or "\x00" in value:
        raise _bad(value, noun, hint)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _bad(value, noun, hint)
    if RESOURCE_ID_RE.fullmatch(value) is None:
        raise _bad(value, noun, hint)
    return value


def validate_hex_id(
    value: str,
    *,
    length: int = 16,
    noun: str = "id",
    hint: str = "Use the id returned by the API.",
) -> str:
    """Validate a generated lowercase-hex id before path/file access."""
    if not isinstance(value, str) or len(value) != length:
        raise _bad(str(value), noun, hint)
    if _HEX_RE.fullmatch(value) is None:
        raise _bad(value, noun, hint)
    return value
