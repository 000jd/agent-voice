"""Audio pipeline: canonicalisation, chunking, stitching, loudness, fit modes.

Every external command is an argv list with ``shell=False``. There is no string
interpolation into a shell anywhere in this project.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .schemas import AgentVoiceError, ErrorCode

SAMPLE_RATE = 24_000

# Sentence terminators for the languages Qwen3-TTS supports, plus paragraph breaks.
_CJK_TERMINATORS = "。！？；"
_LATIN_TERMINATORS = ".!?;"
_SPLIT_RE = re.compile(
    rf"(?<=[{re.escape(_CJK_TERMINATORS + _LATIN_TERMINATORS)}])\s*|(?<=\n)\s*"
)

TARGET_LUFS = -16.0


def _run(argv: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[bytes]:
    """Run an external tool with an argv list. Never a shell string."""
    return subprocess.run(
        argv,
        capture_output=True,
        check=False,
        timeout=timeout,
        shell=False,
    )


def _require_ffmpeg() -> None:
    from shutil import which

    for tool in ("ffmpeg", "ffprobe"):
        if which(tool) is None:
            raise AgentVoiceError(
                ErrorCode.INTERNAL_ERROR,
                f"{tool} not found on PATH",
                "Install ffmpeg; both ffmpeg and ffprobe are required.",
            )


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------


def probe_duration(path: Path) -> float:
    """Exact duration in seconds via ffprobe."""
    _require_ffmpeg()
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    if result.returncode != 0:
        raise AgentVoiceError(
            ErrorCode.INVALID_REF_AUDIO,
            f"ffprobe failed: {result.stderr.decode('utf-8', 'replace')[:300]}",
            "Provide a decodable audio file.",
        )
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise AgentVoiceError(
            ErrorCode.INVALID_REF_AUDIO,
            f"could not read duration: {exc}",
            "Provide a decodable audio file.",
        ) from exc


def canonicalise_ref_audio(
    source: Path,
    destination: Path,
    *,
    trim_silence: bool = True,
) -> float:
    """Convert any decodable input to 24 kHz mono 16-bit WAV, optionally trimmed.

    Returns the resulting duration in seconds.
    """
    _require_ffmpeg()
    destination.parent.mkdir(parents=True, exist_ok=True)

    filters = ["silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.1"]
    if trim_silence:
        filters.append("areverse")
        filters.append("silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.1")
        filters.append("areverse")

    result = _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-sample_fmt",
            "s16",
            "-af",
            ",".join(filters),
            str(destination),
        ]
    )
    if result.returncode != 0:
        raise AgentVoiceError(
            ErrorCode.INVALID_REF_AUDIO,
            f"could not decode reference audio: {result.stderr.decode('utf-8', 'replace')[:300]}",
            "Supply a WAV/MP3/FLAC/OGG file ffmpeg can read.",
        )

    duration = probe_duration(destination)
    if duration <= 0:
        raise AgentVoiceError(
            ErrorCode.INVALID_REF_AUDIO,
            "reference audio is empty after silence trimming",
            "Record at least 3 seconds of continuous speech.",
        )
    return duration


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str


def chunk_text(text: str, *, max_chars: int = 400) -> list[Chunk]:
    """Split on sentence boundaries, then hard-wrap anything still too long.

    Language-aware: handles CJK terminators (。！？；) as well as Latin ones.
    """
    text = text.strip()
    if not text:
        return []

    pieces: list[str] = []
    for raw in _SPLIT_RE.split(text):
        if raw is None:
            continue
        piece = raw.strip()
        if not piece:
            continue
        if len(piece) <= max_chars:
            pieces.append(piece)
            continue
        # Hard wrap long runs, preferring whitespace boundaries.
        remaining = piece
        while len(remaining) > max_chars:
            window = remaining[:max_chars]
            cut = window.rfind(" ")
            if cut < max_chars // 2:
                cut = max_chars
            pieces.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()
        if remaining:
            pieces.append(remaining)

    return [Chunk(index=i, text=p) for i, p in enumerate(pieces) if p]


# ---------------------------------------------------------------------------
# Format conversion / normalisation
# ---------------------------------------------------------------------------


def convert_audio(
    source: Path,
    destination: Path,
    *,
    response_format: str,
    normalize_lufs: float | None = None,
) -> Path:
    """Transcode a WAV into the requested output format."""
    _require_ffmpeg()
    destination.parent.mkdir(parents=True, exist_ok=True)

    argv = ["ffmpeg", "-nostdin", "-y", "-i", str(source)]
    filters: list[str] = []
    if normalize_lufs is not None:
        filters.append(f"loudnorm=I={normalize_lufs}:TP=-1.5:LRA=11")
    if filters:
        argv += ["-af", ",".join(filters)]

    if response_format == "pcm":
        argv += ["-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(SAMPLE_RATE)]
    elif response_format == "mp3":
        argv += ["-codec:a", "libmp3lame", "-q:a", "2"]
    elif response_format == "opus":
        argv += ["-codec:a", "libopus", "-b:a", "64k"]
    elif response_format == "flac":
        argv += ["-codec:a", "flac"]
    elif response_format != "wav":
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"unsupported response_format: {response_format}",
            "Use one of wav, mp3, opus, flac, pcm.",
        )

    argv.append(str(destination))
    result = _run(argv)
    if result.returncode != 0:
        raise AgentVoiceError(
            ErrorCode.INTERNAL_ERROR,
            f"audio conversion failed: {result.stderr.decode('utf-8', 'replace')[:300]}",
            "Check that ffmpeg has the required encoder compiled in.",
        )
    return destination


# ---------------------------------------------------------------------------
# Stitching
# ---------------------------------------------------------------------------


def crossfade_stitch(
    segments: list[Path],
    destination: Path,
    *,
    crossfade_ms: int = 20,
    sample_rate: int = SAMPLE_RATE,
) -> float:
    """Concatenate WAV segments with a short linear crossfade.

    Returns total duration. A 20 ms crossfade removes the audible click you get
    from naive concatenation without smearing consonants.
    """
    import soundfile as sf

    if not segments:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST, "nothing to stitch", "Provide at least one segment."
        )
    if len(segments) == 1:
        data, sr = sf.read(segments[0], dtype="float32", always_2d=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        sf.write(destination, data, sr, subtype="PCM_16")
        return len(data) / sr

    fade = max(1, int(sample_rate * crossfade_ms / 1000))
    accumulated: np.ndarray | None = None

    for segment in segments:
        data, sr = sf.read(segment, dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.mean(axis=1)
        if accumulated is None:
            accumulated = data
            continue

        n = min(fade, len(accumulated), len(data))
        if n > 1:
            ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
            tail = accumulated[-n:] * (1.0 - ramp)
            head = data[:n] * ramp
            accumulated = np.concatenate([accumulated[:-n], tail + head, data[n:]])
        else:
            accumulated = np.concatenate([accumulated, data])

    destination.parent.mkdir(parents=True, exist_ok=True)
    sf.write(destination, accumulated, sample_rate, subtype="PCM_16")
    return len(accumulated) / sample_rate


# ---------------------------------------------------------------------------
# Fit modes for scripted playback
# ---------------------------------------------------------------------------


def fit_segment(
    source: Path,
    destination: Path,
    *,
    target_duration_sec: float,
    fit_mode: str,
) -> float:
    """Force a segment to a target duration using pad or atempo.

    ``atempo`` is clamped to [0.5, 2.0] per filter instance and chained for
    larger ratios, because a single ffmpeg atempo stage refuses extreme values.
    """
    current = probe_duration(source)
    if fit_mode == "none" or target_duration_sec <= 0:
        destination.write_bytes(source.read_bytes())
        return current

    destination.parent.mkdir(parents=True, exist_ok=True)

    if fit_mode == "pad":
        pad_seconds = max(0.0, target_duration_sec - current)
        result = _run(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-af",
                f"apad=pad_dur={pad_seconds:.3f}",
                "-t",
                f"{target_duration_sec:.3f}",
                str(destination),
            ]
        )
        if result.returncode != 0:
            raise AgentVoiceError(
                ErrorCode.INTERNAL_ERROR,
                f"pad fit failed: {result.stderr.decode('utf-8', 'replace')[:200]}",
                "Check ffmpeg apad availability.",
            )
        return target_duration_sec

    if fit_mode == "atempo":
        if current <= 0:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST, "cannot atempo a zero-length segment", ""
            )
        ratio = current / target_duration_sec
        stages: list[str] = []
        remaining = ratio
        while remaining > 2.0:
            stages.append("atempo=2.0")
            remaining /= 2.0
        while remaining < 0.5:
            stages.append("atempo=0.5")
            remaining /= 0.5
        stages.append(f"atempo={remaining:.6f}")

        result = _run(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-af",
                ",".join(stages),
                str(destination),
            ]
        )
        if result.returncode != 0:
            raise AgentVoiceError(
                ErrorCode.INTERNAL_ERROR,
                f"atempo fit failed: {result.stderr.decode('utf-8', 'replace')[:200]}",
                "Check ffmpeg atempo availability.",
            )
        return probe_duration(destination)

    raise AgentVoiceError(
        ErrorCode.BAD_REQUEST,
        f"unknown fit_mode: {fit_mode}",
        "Use none, pad or atempo.",
    )


# ---------------------------------------------------------------------------
# Subtitles
# ---------------------------------------------------------------------------


def _timestamp(seconds: float, *, vtt: bool) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    separator = "." if vtt else ","
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{ms:03d}"


def write_srt(beats: list[tuple[float, float, str]], destination: Path) -> Path:
    lines: list[str] = []
    for index, (start, end, text) in enumerate(beats, start=1):
        lines.append(str(index))
        lines.append(f"{_timestamp(start, vtt=False)} --> {_timestamp(end, vtt=False)}")
        lines.append(text.strip())
        lines.append("")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination


def write_vtt(beats: list[tuple[float, float, str]], destination: Path) -> Path:
    lines = ["WEBVTT", ""]
    for index, (start, end, text) in enumerate(beats, start=1):
        lines.append(str(index))
        lines.append(f"{_timestamp(start, vtt=True)} --> {_timestamp(end, vtt=True)}")
        lines.append(text.strip())
        lines.append("")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination


def estimate_speech_seconds(text: str, *, words_per_minute: float = 165.0) -> float:
    """Rough duration estimate used only for progress reporting, never for timing."""
    words = max(1, len(text.split()))
    return words / words_per_minute * 60.0


def pcm_bytes_to_seconds(num_bytes: int, *, sample_rate: int = SAMPLE_RATE) -> float:
    """int16 mono => 2 bytes per frame."""
    return num_bytes / 2 / sample_rate


def rms_normalise_ratio(block: np.ndarray, target_db: float = -20.0) -> float:
    rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))) or 0.0)
    if rms <= 1e-9:
        return 1.0
    target = 10 ** (target_db / 20.0)
    return min(4.0, max(0.25, target / rms))


def silence(duration_sec: float, *, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    return np.zeros(max(0, int(math.ceil(duration_sec * sample_rate))), dtype=np.float32)
