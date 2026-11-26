"""Deterministic, LRU-bounded synthesis cache.

The cache key hashes the *entire* parameter set that can change the produced
audio. That includes the backend identity, which is why ``engine.yaml`` declares
``backend_name``/``backend_version``: the key must be computable before a worker
is ever spawned (see the architecture note on cache-before-GPU).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from filelock import FileLock

CACHE_KEY_FIELDS: tuple[str, ...] = (
    "text",
    "voice_id",
    "ref_wav_sha256",
    "ref_text",
    "x_vector_only_mode",
    "engine_id",
    "model_alias",
    "backend_name",
    "backend_version",
    "language",
    "seed",
    "speed",
    "response_format",
    "normalize_lufs",
    "chunk_chars",
    "take",
    # v0.3: the same text can be cloned, preset-spoken or designed. The mode,
    # speaker and instruction change the audio just as much as the voice does.
    "mode",
    "speaker",
    "instruct",
    # v0.3.1: the pronunciation map changes the spoken form, so its canonical
    # hash belongs in the key. Updating the lexicon must never reuse old audio.
    "pronunciations",
    "hf_repo",
    "model_revision",
)
"""Every field that can change the audio.

``hf_repo`` and ``model_revision`` are included so that pointing an alias at a
different repository, or pulling updated weights for the same repo, can never
serve audio rendered by the old model. That is worth a cold cache: silently
returning the wrong voice is far worse than re-rendering.

Adding a field here invalidates every existing entry by design — old keys simply
never match again.
"""


INTERNAL_META_KEYS: frozenset[str] = frozenset(
    {"created_at", "last_access", "hits", "bytes", "format"}
)
"""Sidecar keys owned by the cache itself. ``CacheEntry.extra_meta`` exposes
everything *except* these, so a caller only ever sees what it wrote."""


@dataclass(frozen=True)
class CacheEntry:
    key: str
    path: Path
    meta_path: Path
    size_bytes: int
    created_at: float
    hits: int
    extra_meta: dict[str, Any] = field(default_factory=dict)
    """Caller-supplied sidecar metadata (e.g. ``duration_sec``) that must survive
    a cache round-trip. A probe of the encoded file would be wasted work — and
    for a non-WAV container it needs ffprobe — so the producer records the truth
    once at ``put`` time and readers trust it."""


def build_cache_key(**kwargs: Any) -> str:
    """SHA-256 over canonical JSON of the synthesis parameters."""
    missing = [f for f in CACHE_KEY_FIELDS if f not in kwargs]
    if missing:
        raise ValueError(f"cache key is missing required fields: {missing}")

    payload = {name: kwargs[name] for name in CACHE_KEY_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SynthesisCache:
    """File-backed LRU cache.

    Layout::

        <cache_dir>/<sha[:2]>/<sha>.<ext>
        <cache_dir>/<sha[:2]>/<sha>.json

    Total on-disk size is kept under ``max_gb``. Eviction is oldest-first by
    ``last_access``, tracked in the sidecar.
    """

    def __init__(self, cache_dir: Path, max_gb: float = 5.0) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_gb * 1024**3)
        self._lock = FileLock(str(self.cache_dir / ".cache.lock"))

    # -- paths ----------------------------------------------------------
    def _shard(self, key: str) -> Path:
        shard = self.cache_dir / key[:2]
        shard.mkdir(parents=True, exist_ok=True)
        return shard

    def _find_artifact(self, key: str) -> Path | None:
        shard = self.cache_dir / key[:2]
        if not shard.is_dir():
            return None
        for candidate in shard.glob(f"{key}.*"):
            if candidate.suffix != ".json":
                return candidate
        return None

    @staticmethod
    def _extras(meta: dict[str, Any]) -> dict[str, Any]:
        """Caller-supplied sidecar fields, with the cache's own bookkeeping removed."""
        return {name: value for name, value in meta.items() if name not in INTERNAL_META_KEYS}

    # -- read / write ---------------------------------------------------
    def get(self, key: str) -> CacheEntry | None:
        artifact = self._find_artifact(key)
        if artifact is None:
            return None
        meta_path = artifact.with_suffix(".json")
        meta: dict[str, Any] = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}

        meta["last_access"] = time.time()
        meta["hits"] = int(meta.get("hits", 0)) + 1
        try:
            with self._lock:
                meta_path.write_text(json.dumps(meta, sort_keys=True), "utf-8")
                os.utime(artifact, None)
        except OSError:
            pass

        return CacheEntry(
            key=key,
            path=artifact,
            meta_path=meta_path,
            size_bytes=artifact.stat().st_size,
            created_at=float(meta.get("created_at", 0.0)),
            hits=int(meta.get("hits", 0)),
            extra_meta=self._extras(meta),
        )

    def put(
        self,
        key: str,
        source: Path,
        *,
        format: str,
        extra_meta: dict[str, Any] | None = None,
    ) -> CacheEntry:
        """Copy a finished artifact into the cache, then enforce the size bound.

        ``extra_meta`` is merged into the sidecar JSON and handed back on every
        later :meth:`get`, so a producer can record facts about the audio (true
        duration, sample rate, chunk count) once instead of re-probing the file
        on each hit. Cache-owned keys (``INTERNAL_META_KEYS``) always win.
        """
        shard = self._shard(key)
        target = shard / f"{key}.{format}"
        meta_path = shard / f"{key}.json"
        extras = dict(extra_meta or {})

        with self._lock:
            tmp = target.with_suffix(f".{format}.tmp")
            with open(source, "rb") as src, open(tmp, "wb") as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
            os.replace(tmp, target)

            now = time.time()
            meta = {
                **extras,
                "created_at": now,
                "last_access": now,
                "hits": 0,
                "bytes": target.stat().st_size,
                "format": format,
            }
            meta_path.write_text(json.dumps(meta, sort_keys=True), "utf-8")

        self.evict_lru()
        return CacheEntry(
            key=key,
            path=target,
            meta_path=meta_path,
            size_bytes=target.stat().st_size,
            created_at=now,
            hits=0,
            extra_meta=self._extras(extras),
        )

    # -- eviction -------------------------------------------------------
    def total_bytes(self) -> int:
        total = 0
        for path in self.cache_dir.rglob("*"):
            if path.is_file() and path.suffix not in {".lock", ".json"}:
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        return total

    def evict_lru(self) -> list[str]:
        """Drop oldest entries until the cache fits. Returns evicted keys."""
        entries: list[tuple[float, Path, str]] = []
        for artifact in self.cache_dir.rglob("*"):
            if not artifact.is_file() or artifact.suffix in {".lock", ".json", ".tmp"}:
                continue
            meta_path = artifact.with_suffix(".json")
            last_access = artifact.stat().st_mtime
            if meta_path.exists():
                try:
                    last_access = float(
                        json.loads(meta_path.read_text("utf-8")).get("last_access", last_access)
                    )
                except (OSError, json.JSONDecodeError, ValueError):
                    pass
            entries.append((last_access, artifact, artifact.stem))

        total = sum(e[1].stat().st_size for e in entries)
        if total <= self.max_bytes:
            return []

        evicted: list[str] = []
        for _last_access, artifact, key in sorted(entries):
            if total <= self.max_bytes:
                break
            size = artifact.stat().st_size
            with self._lock:
                artifact.unlink(missing_ok=True)
                artifact.with_suffix(".json").unlink(missing_ok=True)
            total -= size
            evicted.append(key)
        return evicted

    def clear(self) -> None:
        for path in self.cache_dir.rglob("*"):
            if path.is_file() and path.suffix not in {".lock"}:
                path.unlink(missing_ok=True)
