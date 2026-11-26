"""Kokoro-specific facts the daemon needs without importing the engine stack.

Two consumers, both in the daemon process:

* the OpenAI-compatible routes translate OpenAI's builtin voice names onto the
  eight Kokoro preset speakers this engine advertises (``KOKORO_OPENAI_VOICES``);
* ``agent-voice doctor`` reports whether ``espeak-ng`` is installed
  (:func:`espeak_ng_status`).

**This module must stay import-safe.** It is imported by ``cli.py`` and the API
app, so it may only use the standard library: no ``torch``, ``kokoro``,
``transformers`` or ``misaki`` import, at module scope or lazily. Importing torch
costs ~1 s and pins ~200 MB of RSS in the *daemon*, which is exactly what the
zero-GPU Kokoro design avoids.

The engine side lives in ``engines/kokoro/worker.py``; the numbers below mirror
``engines/kokoro/engine.yaml`` and are asserted by the Kokoro tests so the two
cannot drift apart silently.
"""

from __future__ import annotations

import re
import shutil
import subprocess

ENGINE_ID = "kokoro"
"""Engine id, identical to the directory name and to ``worker.py``'s hello."""

BACKEND_NAME = "kokoro"
"""PyPI distribution name of the backend; part of the synthesis cache key."""

MODEL_ALIAS = "kokoro-82m"
"""The manifest's model alias, used in ``models download kokoro kokoro-82m``."""

HF_REPO = "hexgrad/Kokoro-82M"
"""HuggingFace repo in the cache layout ``hf_model_downloaded()`` understands."""

SAMPLE_RATE = 24_000
"""Native rate. Kokoro decodes 24 kHz mono; nothing is resampled to reach it."""

ESPEAK_NG_BINARY = "espeak-ng"

KOKORO_OPENAI_VOICES: dict[str, str] = {
    "alloy": "af_heart",
    "echo": "am_adam",
    "nova": "af_bella",
    "onyx": "am_michael",
    "fable": "bm_george",
    "shimmer": "bf_emma",
}
"""OpenAI builtin voice name -> Kokoro preset speaker.

Every OpenAI-shaped client (SDK, LangChain, Pipecat, LiveKit) hard-codes these
six names, so an unmodified client must hear speech rather than a 404. The
mapping is by perceived register, not by a scientific match: ``alloy`` and
``nova`` are the bright female presets, ``echo``/``onyx`` the male ones, and
``fable``/``shimmer`` cover the British set. It only ever applies to a Kokoro
model, because the speaker names are meaningless on any other engine.
"""

KOKORO_PRESET_SPEAKERS: tuple[str, ...] = (
    "af_heart",
    "af_bella",
    "af_sarah",
    "am_adam",
    "am_michael",
    "bf_emma",
    "bm_george",
    "bm_lewis",
)
"""The preset speakers ``engine.yaml`` advertises. ``a`` = American English,
``b`` = British English; the first letter is also Kokoro's ``lang_code``."""

DEFAULT_KOKORO_VOICE = KOKORO_OPENAI_VOICES["alloy"]
"""Fallback speaker when a caller names a voice this engine cannot serve."""

_OPENAI_TO_KOKORO = KOKORO_OPENAI_VOICES
"""Alias kept private so the public map cannot be rebound by a caller."""

_VERSION_PATTERN = re.compile(r"(\d+(?:\.\d+)+)")


def openai_voice_to_kokoro(voice: str | None) -> str | None:
    """The Kokoro preset for an OpenAI builtin voice name, or ``None``.

    ``None`` means "not one of the six builtin names", which the caller must
    treat as an unknown voice rather than silently substituting a timbre: a
    typo should fail loudly, not speak in the wrong voice.
    """
    return _OPENAI_TO_KOKORO.get((voice or "").strip().lower())


def is_preset_speaker(voice: str | None) -> bool:
    """True when ``voice`` is one of the speakers the manifest advertises."""
    return (voice or "").strip() in KOKORO_PRESET_SPEAKERS


def espeak_ng_status(timeout: float = 10.0) -> tuple[str | None, str | None]:
    """``(path, version)`` for ``espeak-ng``, each ``None`` when unavailable.

    Kokoro's English G2P is spaCy-based; espeak-ng is the out-of-dictionary
    fallback (and the backend for every non-English voice). English mostly works
    without it, so a missing binary is a WARN in ``doctor``, not a FAIL — but an
    agent that needs an unusual word spelled "normally" will notice.

    Read-only by construction: only ``--version`` is ever executed, with an argv
    list, ``shell=False`` and a timeout, so a PATH entry that is not espeak-ng
    cannot turn a health check into arbitrary command execution.
    """
    path = shutil.which(ESPEAK_NG_BINARY)
    if path is None:
        return None, None

    argv = [path, "--version"]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Found on PATH but not runnable (bad symlink, wrong arch, wedged): the
        # binary exists, its version does not.
        return path, None

    output = f"{getattr(result, 'stdout', '') or ''}\n{getattr(result, 'stderr', '') or ''}"
    match = _VERSION_PATTERN.search(output)
    return path, (match.group(1) if match else None)
