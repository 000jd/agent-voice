"""Line-preserving ``.env`` editing for ``agent-voice setup``.

The writer's contract, in order of importance:

1. **Never lose a line.** Comments, blank lines, unknown keys and their order are
   preserved byte-for-byte. Only the values of the keys being set change, and a
   commented-out assignment (``# DATA_DIR=./data``, the v0.5.0 example) is
   promoted in place instead of being duplicated at the bottom of the file.
2. **Never overwrite a backup.** ``.env`` is copied to ``.env.bak``; if that
   already exists the copy goes to ``.env.<UTC stamp>.bak``, and a second
   collision gets a numeric suffix. The backup is created with
   ``O_CREAT|O_EXCL``, so a concurrent run cannot clobber one either.
3. **Never leak a secret.** ``AGENT_VOICE_API_KEY`` is read like any other key but
   never rendered: :meth:`EnvDiff.lines` prints ``(hidden)`` for it. Nothing in
   this module echoes a value it did not just set.
4. **Atomic and private.** The new file is written to a temp sibling, ``fsync``-ed,
   ``chmod 0600``-ed and ``os.replace``-d over the target (the
   ``engine_registry._atomic_write_text`` pattern). A crash leaves the old file
   intact, and the result is never group/world readable: a ``.env`` can hold an
   API key.

Nothing here imports typer, prints to a stream or reads stdin: the CLI owns the
output, this module owns the bytes.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

ENV_MODE = 0o600
"""Mode for every file this module writes: ``.env`` and its backups."""

SECRET_KEYS = frozenset({"AGENT_VOICE_API_KEY"})
"""Keys whose value is never rendered. Add here, never at a call site."""

MASK = "(hidden)"

DEFAULT_BANNER = "# --- agent-voice setup ---"
"""Section header used when a key has no mapped banner."""

KEY_BANNERS: dict[str, str] = {
    "AGENT_VOICE_HOME": "# --- Paths ---",
    "DATA_DIR": "# --- Paths ---",
    "ENGINES_DIR": "# --- Paths ---",
    "AGENT_VOICE_DEVICE": "# --- Hardware ---",
    "AGENT_VOICE_DEVICES": "# --- Hardware ---",
    "AGENT_VOICE_DEFAULT_ENGINE": "# --- Defaults ---",
    "AGENT_VOICE_DEFAULT_ASR_ENGINE": "# --- Defaults ---",
}
"""Which section a newly appended key belongs to (matches ``.env.example``)."""

_ASSIGNMENT = re.compile(
    r"^(?P<prefix>[ \t]*)(?P<export>export[ \t]+)?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=(?P<value>.*)$"
)
_COMMENTED_ASSIGNMENT = re.compile(
    r"^[ \t]*#[ \t]*(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=(?P<value>.*)$"
)
_SECTION_LINE = re.compile(r"^[ \t]*#[ \t]*-{2,}")

EnvAction = Literal["add", "update", "same"]


@dataclass(frozen=True)
class EnvChange:
    """One key's fate: added, updated, or already correct."""

    key: str
    old: str | None
    new: str
    action: EnvAction = "add"
    from_comment: bool = False
    """True when a commented-out assignment was promoted to a real one."""

    @property
    def pending(self) -> bool:
        return self.action != "same"


@dataclass(frozen=True)
class EnvDiff:
    """What :func:`write_env` would change, in the order the keys were planned."""

    path: Path
    changes: tuple[EnvChange, ...] = ()

    @property
    def pending(self) -> tuple[EnvChange, ...]:
        return tuple(change for change in self.changes if change.pending)

    @property
    def has_changes(self) -> bool:
        return bool(self.pending)

    def lines(self) -> list[str]:
        """Render the diff for a terminal, masking every secret value."""
        rendered: list[str] = []
        for change in self.changes:
            if change.key in SECRET_KEYS:
                rendered.append(f"{change.action:<6} {change.key}={MASK}")
                continue
            if change.action == "add":
                rendered.append(f"{change.action:<6} {change.key}={change.new}")
            elif change.action == "same":
                rendered.append(f"{change.action:<6} {change.key}={change.new} (unchanged)")
            else:
                from_comment = " (from a commented line)" if change.from_comment else ""
                rendered.append(
                    f"{change.action:<6} {change.key}: {change.old} -> {change.new}"
                    f"{from_comment}"
                )
        return rendered


@dataclass(frozen=True)
class EnvWriteResult:
    path: Path
    backup: Path | None = None
    diff: EnvDiff = EnvDiff(path=Path(".env"))
    written: bool = False
    mode_fixed: bool = False
    """True when only the file mode was tightened; the content was left alone."""


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        inner = value[1:-1]
        return inner.replace('\\"', '"') if value[0] == '"' else inner
    return value


def _format_value(value: str) -> str:
    """Quote a value dotenv would otherwise mis-parse.

    A bare ``#`` starts a comment and whitespace is trimmed, so any value
    containing one is written quoted. Everything this module sets today is a
    path or an engine id, but the writer must not corrupt an arbitrary value.
    """
    if value == "" or re.search(r"[\s#'\"]", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def parse_env_text(text: str) -> dict[str, str]:
    """The effective ``KEY=value`` map of a ``.env`` body.

    Deliberately forgiving: unparseable lines are ignored rather than raised on,
    because the file is also hand-edited and a broken line must not stop setup
    from reporting the rest.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if match is None:
            continue
        values[match.group("key")] = _unquote(match.group("value"))
    return values


def read_env(path: Path) -> dict[str, str]:
    """Read ``path`` as a ``.env`` map; a missing file is an empty map."""
    try:
        return parse_env_text(path.read_text("utf-8"))
    except OSError:
        return {}


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def _insert_into_section(lines: list[str], banner: str, block: Sequence[str]) -> bool:
    """Insert ``block`` at the end of the named ``# --- ... ---`` section.

    Matched by prefix, not equality: ``.env.example`` writes ``# --- Paths ----``
    with a rule of dashes, and a key appended by setup must still land inside that
    section instead of growing a second one.
    """
    for index, line in enumerate(lines):
        if not line.strip().startswith(banner):
            continue
        end = len(lines)
        for later in range(index + 1, len(lines)):
            if _SECTION_LINE.match(lines[later]):
                end = later
                break
        insert_at = end
        while insert_at > index + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        trailer = [""] if insert_at < len(lines) else []
        lines[insert_at:insert_at] = list(block) + trailer
        return True
    return False


def render_env_text(
    text: str, updates: Mapping[str, str]
) -> tuple[str, tuple[EnvChange, ...]]:
    """Apply ``updates`` to a ``.env`` body, preserving every other line.

    Returns the new body and the per-key changes. Pure: no filesystem access, so
    the planner can show the diff a dry run would write without writing it.
    """
    separator = "\r\n" if "\r\n" in text else "\n"
    kept_trailing_newline = text.endswith(separator) if text else True
    lines = text.splitlines()
    new_lines = list(lines)
    changes: dict[str, EnvChange] = {}

    def record(key: str, old: str | None, *, from_comment: bool = False) -> None:
        new = updates[key]
        action: EnvAction = "same" if old == new else ("add" if old is None else "update")
        changes[key] = EnvChange(
            key=key, old=old, new=new, action=action, from_comment=from_comment
        )

    # 1. Active assignments: rewrite the value, keep indentation and `export`.
    for index, line in enumerate(lines):
        match = _ASSIGNMENT.match(line)
        if match is None:
            continue
        key = match.group("key")
        if key not in updates:
            continue
        prefix = f"{match.group('prefix')}{match.group('export') or ''}"
        new_lines[index] = f"{prefix}{key}={_format_value(updates[key])}"
        if key not in changes:
            record(key, _unquote(match.group("value")))

    # 2. Commented-out assignments: promote in place, the way a migrated
    #    `# DATA_DIR=./data` line from the v0.5.0 example must behave.
    for index, line in enumerate(lines):
        if _ASSIGNMENT.match(line) is not None:
            continue
        match = _COMMENTED_ASSIGNMENT.match(line)
        if match is None:
            continue
        key = match.group("key")
        if key not in updates or key in changes:
            continue
        new_lines[index] = f"{key}={_format_value(updates[key])}"
        record(key, _unquote(match.group("value")), from_comment=True)

    # 3. Everything else is appended under its section banner.
    grouped: dict[str, list[str]] = {}
    for key in updates:
        if key in changes:
            continue
        grouped.setdefault(KEY_BANNERS.get(key, DEFAULT_BANNER), []).append(key)
    for banner, keys in grouped.items():
        block = [f"{key}={_format_value(updates[key])}" for key in keys]
        if not _insert_into_section(new_lines, banner, block):
            if new_lines and new_lines[-1].strip():
                new_lines.append("")
            new_lines.append(banner)
            new_lines.extend(block)
        for key in keys:
            record(key, None)

    body = separator.join(new_lines)
    if kept_trailing_newline and new_lines:
        body += separator
    ordered = tuple(changes[key] for key in updates if key in changes)
    return body, ordered


def plan_env_update(path: Path, updates: Mapping[str, str]) -> EnvDiff:
    """What :func:`write_env` would do — a read-only dry run of the diff."""
    try:
        text = path.read_text("utf-8")
    except OSError:
        text = ""
    _body, changes = render_env_text(text, updates)
    return EnvDiff(path=path, changes=changes)


# ---------------------------------------------------------------------------
# backups and writing
# ---------------------------------------------------------------------------


def _utc_stamp(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def _backup_candidates(path: Path, now: datetime | None) -> Iterator[Path]:
    """Every backup name to try, in order. ``.bak`` first, then timestamped."""
    yield path.with_name(f"{path.name}.bak")
    stamp = _utc_stamp(now)
    yield path.with_name(f"{path.name}.{stamp}.bak")
    for counter in range(2, 100):
        yield path.with_name(f"{path.name}.{stamp}-{counter}.bak")


def backup_path(path: Path, *, now: datetime | None = None) -> Path:
    """The path the next backup would be written to. Creates nothing."""
    for candidate in _backup_candidates(path, now):
        if not candidate.exists():
            return candidate
    raise OSError(f"cannot find a free backup name next to {path}")


def _write_backup(path: Path, text: str, *, now: datetime | None = None) -> Path:
    """Copy ``text`` beside ``path``, never overwriting an existing backup."""
    for candidate in _backup_candidates(path, now):
        try:
            descriptor = os.open(
                candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, ENV_MODE
            )
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        return candidate
    raise OSError(f"cannot find a free backup name next to {path}")


def atomic_write_text(path: Path, content: str, *, mode: int = ENV_MODE) -> None:
    """Write ``content`` to ``path`` atomically, with an explicit mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tmp-", suffix=path.suffix
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_env(
    path: Path, updates: Mapping[str, str], *, now: datetime | None = None
) -> EnvWriteResult:
    """Update ``path`` in place, backing it up first; returns what changed.

    A file whose values already match is not rewritten at all: only its mode is
    tightened to :data:`ENV_MODE`, so an idempotent ``setup`` re-run leaves the
    mtime (and any editor buffer) alone.
    """
    try:
        existing: str | None = path.read_text("utf-8")
    except OSError:
        existing = None

    body, changes = render_env_text(existing or "", updates)
    diff = EnvDiff(path=path, changes=changes)

    if existing is not None and not diff.has_changes:
        mode_fixed = False
        try:
            if path.stat().st_mode & 0o777 != ENV_MODE:
                os.chmod(path, ENV_MODE)
                mode_fixed = True
        except OSError:
            pass
        return EnvWriteResult(path=path, diff=diff, written=False, mode_fixed=mode_fixed)

    backup = _write_backup(path, existing, now=now) if existing is not None else None
    atomic_write_text(path, body, mode=ENV_MODE)
    return EnvWriteResult(path=path, backup=backup, diff=diff, written=True)
