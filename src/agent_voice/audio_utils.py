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


_BANNER_PREFIXES = (
    "ffmpeg version",
    "ffprobe version",
    "configuration:",
    "built with",
    "libav",
    "Input #0",
    "Output #0",
    "Stream mapping:",
    "Press [q]",
    "  Metadata:",
    "    encoder",
    "    handler_name",
    "    vendor_id",
    "    creation_time",
)


def _ffmpeg_error_tail(stderr: bytes, *, lines: int = 3, limit: int = 250) -> str:
    """The useful end of an ffmpeg/ffprobe stderr, without the build banner.

    ffmpeg always prints its version, compiled-in ``configuration:`` line and
    stream metadata before the actual complaint, so a naive ``stderr[:300]``
    shows an agent the build flags instead of what went wrong. Keep the last few
    meaningful lines and cap the length.
    """
    text = stderr.decode("utf-8", errors="replace")
    meaningful = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(_BANNER_PREFIXES)
    ]
    tail = " | ".join(meaningful[-lines:]) if meaningful else text.strip()
    return tail[:limit]


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
            f"ffprobe failed: {_ffmpeg_error_tail(result.stderr)}",
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
            f"could not decode reference audio: {_ffmpeg_error_tail(result.stderr)}",
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
            f"audio conversion failed: {_ffmpeg_error_tail(result.stderr)}",
            "Check that ffmpeg has the required encoder compiled in.",
        )
    return destination


# ---------------------------------------------------------------------------
# Stitching
# ---------------------------------------------------------------------------


CROSSFADE_MS = 20
"""Default seam crossfade: long enough to hide a click, short enough not to smear
consonants. Every consumer that needs to reason about seams shares this value."""

PEAK_CEILING = 0.89
"""≈ -1 dBFS. Emphasis gain is capped here so a loud syllable cannot clip."""


def crossfade_stitch(
    segments: list[Path],
    destination: Path,
    *,
    crossfade_ms: int = CROSSFADE_MS,
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


def stitch_offsets(
    frame_counts: list[int] | tuple[int, ...],
    *,
    crossfade_ms: int = CROSSFADE_MS,
    sample_rate: int = SAMPLE_RATE,
) -> list[tuple[float, float]]:
    """``(onset_sec, span_sec)`` per stitched component.

    Mirrors :func:`crossfade_stitch`'s arithmetic exactly: it removes
    ``min(fade, accumulated, next)`` frames at each seam, and only when that is
    more than one frame. Summing raw durations instead would drift steadily past
    the real file length, so any caller that reports timings has to use this.

    The last component's span is its remaining frames (the tail is never faded),
    which makes the spans add up to the stitched length.
    """
    counts = [max(0, int(count)) for count in frame_counts]
    if not counts:
        return []
    fade = max(1, int(sample_rate * crossfade_ms / 1000))
    losses: list[int] = []
    accumulated = counts[0]
    for count in counts[1:]:
        overlap = min(fade, accumulated, count)
        loss = overlap if overlap > 1 else 0
        losses.append(loss)
        accumulated += count - loss

    offsets: list[tuple[float, float]] = []
    cursor = 0
    for position, count in enumerate(counts):
        loss = losses[position] if position < len(losses) else 0
        span = max(0, count - loss)
        offsets.append((cursor / sample_rate, span / sample_rate))
        cursor += span
    return offsets


def apply_gain(
    block: np.ndarray,
    gain_db: float,
    *,
    peak_ceiling: float = PEAK_CEILING,
) -> np.ndarray:
    """Scale a float block by ``gain_db``, backed off if the peak would clip.

    ``[emphasis]`` adds a few dB on purpose. Louder is the point; distorted is
    not, so the result is peak-limited to ``peak_ceiling`` rather than clipped by
    whichever encoder sees it next.
    """
    if gain_db == 0 or block.size == 0:
        return block
    scaled = block * np.float32(10.0 ** (gain_db / 20.0))
    peak = float(np.max(np.abs(scaled)))
    if peak > peak_ceiling > 0:
        scaled = scaled * np.float32(peak_ceiling / peak)
    return scaled.astype(np.float32, copy=False)


def apply_gain_file(
    source: Path,
    destination: Path,
    gain_db: float,
    *,
    peak_ceiling: float = PEAK_CEILING,
) -> Path:
    """Write ``source`` to ``destination`` with ``gain_db`` applied (PCM_16 WAV)."""
    import soundfile as sf

    data, sample_rate = sf.read(source, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sf.write(destination, apply_gain(data, gain_db, peak_ceiling=peak_ceiling), sample_rate,
             subtype="PCM_16")
    return destination


def scale_pcm16(chunk: bytes, gain_db: float, *, peak_ceiling: float = PEAK_CEILING) -> bytes:
    """Apply ``gain_db`` to one chunk of 24 kHz mono int16 LE PCM.

    Used by the streaming path, which has no file to rewrite at stitch time. Each
    chunk is limited on its own, so a long loud passage cannot clip even though
    there is no look-ahead.
    """
    if gain_db == 0 or not chunk:
        return chunk
    samples = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0
    scaled = apply_gain(samples, gain_db, peak_ceiling=peak_ceiling) * 32768.0
    return np.clip(np.round(scaled), -32768, 32767).astype("<i2").tobytes()


def silence_pcm16(duration_sec: float, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    """``duration_sec`` of int16 LE silence, for inserting pauses into a stream."""
    frames = max(0, int(round(duration_sec * sample_rate)))
    return bytes(frames * 2)


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
                f"pad fit failed: {_ffmpeg_error_tail(result.stderr)}",
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
                f"atempo fit failed: {_ffmpeg_error_tail(result.stderr)}",
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
