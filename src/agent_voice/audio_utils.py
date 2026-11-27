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


# ---------------------------------------------------------------------------
# Media inspection and extraction (v0.5: transcription and dubbing)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaInfo:
    """What ffprobe can say about a file before we decode a single frame."""

    path: Path
    duration_sec: float
    has_audio: bool
    has_video: bool
    sample_rate: int
    channels: int


def probe_media(path: Path) -> MediaInfo:
    """Stream layout plus duration, for admission before any work happens.

    Both v0.5 surfaces have to answer "is there anything to listen to, and how
    long is it" *before* they spawn a worker or decode an hour of video: a caller
    who hands us a silent screen recording or a three-hour lecture must get a
    fast, specific refusal instead of a wasted ASR run.

    A probe that cannot read the container is reported as ``INVALID_REF_AUDIO``
    (the existing code for "this media cannot be decoded"), because the fix is
    the same one the clone path asks for: supply something ffmpeg understands.
    """
    _require_ffmpeg()
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
    )
    if result.returncode != 0:
        raise AgentVoiceError(
            ErrorCode.INVALID_REF_AUDIO,
            f"could not probe {Path(path).name}: {_ffmpeg_error_tail(result.stderr)}",
            "Provide a file ffmpeg can demux (mp4, mkv, mov, wav, mp3, ...). If the "
            "file is fine, check that ffmpeg/ffprobe are installed and on PATH.",
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AgentVoiceError(
            ErrorCode.INVALID_REF_AUDIO,
            f"could not parse ffprobe output for {Path(path).name}: {exc}",
            "Provide a file ffmpeg can demux; a truncated or non-media file is the "
            "usual cause.",
        ) from exc

    streams = payload.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    video = next((s for s in streams if s.get("codec_type") == "video"), None)

    duration = 0.0
    try:
        duration = float((payload.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        # Some containers carry the duration only on the stream (a raw segment, an
        # mpeg-ts off an encoder). The longest stream is the honest answer.
        for stream in streams:
            try:
                duration = max(duration, float(stream.get("duration") or 0.0))
            except (TypeError, ValueError):
                continue

    def _int(value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    return MediaInfo(
        path=Path(path),
        duration_sec=duration,
        has_audio=audio is not None,
        has_video=video is not None,
        sample_rate=_int((audio or {}).get("sample_rate")),
        channels=_int((audio or {}).get("channels")),
    )


def extract_asr_wav(source: Path, destination: Path) -> float:
    """Decode ``source`` to the 16 kHz mono 16-bit WAV an ASR backend expects.

    Phonon's front end is fixed at 16 kHz mono, and resampling here means the
    worker never has to depend on the exact container it was handed. Always
    converting (rather than checking the source format first) is one decode pass
    and removes a whole class of "the source happened to be 44.1 kHz stereo"
    bug, at a cost that is negligible next to the transcription itself.

    Returns the extracted duration in seconds.
    """
    _require_ffmpeg()
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            str(destination),
        ],
        timeout=600.0,
    )
    if result.returncode != 0:
        raise AgentVoiceError(
            ErrorCode.INVALID_REF_AUDIO,
            f"could not extract audio from {Path(source).name}: "
            f"{_ffmpeg_error_tail(result.stderr)}",
            "Provide a file with a decodable audio stream.",
        )
    return probe_duration(destination)


def extract_bed(source: Path, destination: Path) -> float:
    """Decode ``source``'s audio to a 24 kHz stereo 16-bit WAV bed.

    The bed is resampled exactly once, here, so the later sidechain mix never
    resamples per line: ffmpeg would otherwise re-convert the bed for every
    ``amix``/``sidechaincompress`` invocation, and the dub's only audio quality
    decision would be made as a side effect of the filter graph.
    """
    _require_ffmpeg()
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "2",
            "-ar",
            str(SAMPLE_RATE),
            "-sample_fmt",
            "s16",
            str(destination),
        ],
        timeout=600.0,
    )
    if result.returncode != 0:
        raise AgentVoiceError(
            ErrorCode.INVALID_REF_AUDIO,
            f"could not extract the original bed from {Path(source).name}: "
            f"{_ffmpeg_error_tail(result.stderr)}",
            "Provide a file with a decodable audio stream.",
        )
    return probe_duration(destination)


def slice_clip(
    source: Path,
    destination: Path,
    *,
    start_sec: float,
    duration_sec: float,
) -> float:
    """Cut ``duration_sec`` out of ``source`` starting at ``start_sec``.

    Input-seek (``-ss`` before ``-i``) rather than output-seek: the speaker
    reference is sliced out of a file that can be an hour long, and decoding up to
    the offset to throw it away is the difference between an instant cut and a
    real-time wait.

    The cut keeps the source's sample format on purpose — it is a cut, not a
    conversion; :func:`canonicalise_ref_audio` is the one place that decides the
    final reference format.
    """
    _require_ffmpeg()
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-ss",
            f"{max(0.0, start_sec):.3f}",
            "-t",
            f"{max(0.0, duration_sec):.3f}",
            "-i",
            str(source),
            str(destination),
        ],
        timeout=600.0,
    )
    if result.returncode != 0:
        raise AgentVoiceError(
            ErrorCode.INVALID_REF_AUDIO,
            f"could not slice {Path(source).name}: {_ffmpeg_error_tail(result.stderr)}",
            "Check the source is decodable and the window is inside its duration.",
        )
    return probe_duration(destination)


def _atempo_stages(ratio: float) -> list[str]:
    """Chain ``atempo`` stages so one filter instance never sees an extreme value.

    ffmpeg's ``atempo`` accepts [0.5, 2.0] per instance, so a bigger ratio is
    expressed as repeated 2.0 stages and the residual. This mirrors
    :func:`fit_segment`'s algorithm; dubbing bounds its ratio by ``max_atempo``
    (<= 2.0) so a single stage is normally enough, but the loop is written to hold
    for the general case rather than assuming that bound.
    """
    stages: list[str] = []
    remaining = ratio
    while remaining > 2.0:
        stages.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        stages.append("atempo=0.5")
        remaining /= 0.5
    stages.append(f"atempo={remaining:.6f}")
    return stages


def atempo_to_duration(
    source: Path,
    destination: Path,
    *,
    target_sec: float,
    max_atempo: float = 1.35,
) -> tuple[float, bool]:
    """Speed ``source`` up (if needed) to fit ``target_sec``.

    Returns ``(atempo_used, still_overflows)``. A line shorter than its slot is
    re-encoded unchanged (``atempo`` 1.0): dubbing must never *slow* a line down
    to fill the slot, because a stretched line sounds like a different take and
    the gap after it is what the picture is timed against anyway.

    When the required ratio exceeds ``max_atempo`` the ceiling is applied and
    ``still_overflows`` is True, so the caller can report the line instead of
    truncating it — clipped audio is worse than a slightly long line, and the
    caller can only fix what it is told about.

    Output is 24 kHz mono PCM s16: the mix stage takes exactly that.
    """
    current = probe_duration(source)
    if target_sec <= 0:
        # No room at all. Reporting an overflow is the only truthful answer; there
        # is no ratio that fits a zero-length slot.
        atempo_used, still_overflows = 1.0, True
    else:
        ratio = current / target_sec
        if ratio <= 1.0:
            atempo_used, still_overflows = 1.0, False
        elif ratio > max_atempo:
            atempo_used, still_overflows = float(max_atempo), True
        else:
            atempo_used, still_overflows = ratio, False

    destination.parent.mkdir(parents=True, exist_ok=True)
    argv = ["ffmpeg", "-nostdin", "-y", "-i", str(source)]
    if atempo_used != 1.0:
        argv += ["-af", ",".join(_atempo_stages(atempo_used))]
    argv += [
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-sample_fmt",
        "s16",
        str(destination),
    ]
    result = _run(argv)
    if result.returncode != 0:
        raise AgentVoiceError(
            ErrorCode.INTERNAL_ERROR,
            f"could not fit {Path(source).name} to {target_sec:.3f}s: "
            f"{_ffmpeg_error_tail(result.stderr)}",
            "Check ffmpeg atempo availability.",
        )
    return atempo_used, still_overflows


def place_on_timeline(
    clips: list[tuple[float, Path]],
    destination: Path,
    *,
    duration_sec: float,
    sample_rate: int = SAMPLE_RATE,
) -> Path:
    """Mix timed clips into one buffer of exactly ``duration_sec``.

    Dubbing is a placement problem, not a stitching problem: every line belongs at
    the source segment's start time, and two lines must be able to overlap without
    one being dropped. Adding into a preallocated buffer gives that for free, and
    an all-zero buffer of the media's length means a line that never got rendered
    is heard as silence in its slot rather than shifting everything after it.

    Overlapping lines sum, so the result is peak-limited to :data:`PEAK_CEILING`
    afterwards (scaled down only — never amplified, which would make a quiet dub
    clip after the limiter).
    """
    import soundfile as sf

    total_frames = max(1, int(math.ceil(max(0.0, duration_sec) * sample_rate)))
    buffer = np.zeros(total_frames, dtype=np.float32)

    for start_sec, clip in clips:
        data, clip_rate = sf.read(clip, dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.mean(axis=1)
        if clip_rate != sample_rate:
            # Lines arrive at SAMPLE_RATE, but a stray rate would otherwise be
            # placed at the wrong tempo *and* the wrong length. Silent mis-timing is
            # the one failure mode this helper exists to rule out.
            stretched = int(round(len(data) * sample_rate / clip_rate))
            positions = np.linspace(0.0, float(len(data)), num=max(1, stretched), endpoint=False)
            data = np.interp(
                positions,
                np.arange(len(data), dtype=np.float64),
                data.astype(np.float64),
            ).astype(np.float32)

        offset = int(round(start_sec * sample_rate))
        if offset < 0:
            # Defensive: admission rejects a negative start, but a partial clip is
            # better than placing the whole line at zero.
            data = data[-offset:]
            offset = 0
        if offset >= total_frames:
            # A clip that starts past the end of the media contributes nothing. It
            # is skipped rather than raising: the caller validated its slots against
            # the media duration already.
            continue
        end = min(total_frames, offset + len(data))
        buffer[offset:end] += data[: end - offset]

    peak = float(np.max(np.abs(buffer))) if buffer.size else 0.0
    if peak > PEAK_CEILING:
        buffer = buffer * np.float32(PEAK_CEILING / peak)

    destination.parent.mkdir(parents=True, exist_ok=True)
    sf.write(destination, buffer, sample_rate, subtype="PCM_16")
    return destination


def mix_bed_with_voice(
    bed: Path,
    voice: Path,
    destination: Path,
    *,
    duck_db: float = -18.0,
    fallback: bool = True,
) -> tuple[Path, bool]:
    """Mix the dubbed voice over the original bed, ducking it under the speech.

    Returns ``(destination, ducked)``. Sidechain compression is what makes a dub
    listenable: music and effects stay present between lines but drop out from
    under the dialogue, instead of forcing the caller to choose between a
    buried voice and a background that is gone.

    ``duck_db`` is the sidechain threshold, converted from dB to the linear
    amplitude ffmpeg's ``threshold`` takes — so the knob actually moves the
    ducking rather than merely decorating the call.

    A score built without ``sidechaincompress`` (an older ffmpeg, or a build with
    the filter compiled out) still gets a usable dub: the plain ``amix`` fallback
    is returned with ``ducked=False`` so the caller knows the bed is at full level.
    """
    _require_ffmpeg()
    destination.parent.mkdir(parents=True, exist_ok=True)

    threshold = 10 ** (duck_db / 20.0)
    ducked_filter = (
        "[1:a]asplit=2[v1][sc];"
        f"[0:a][sc]sidechaincompress=threshold={threshold:.6f}:ratio=8:"
        "attack=20:release=300[bed];"
        "[bed][v1]amix=inputs=2:duration=first:dropout_transition=0,"
        "alimiter=limit=0.97[a]"
    )
    result = _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(bed),
            "-i",
            str(voice),
            "-filter_complex",
            ducked_filter,
            "-map",
            "[a]",
            "-ac",
            "2",
            "-ar",
            str(SAMPLE_RATE),
            "-sample_fmt",
            "s16",
            str(destination),
        ],
        timeout=600.0,
    )
    if result.returncode == 0:
        return destination, True
    if not fallback:
        raise AgentVoiceError(
            ErrorCode.INTERNAL_ERROR,
            f"bed/voice mix failed: {_ffmpeg_error_tail(result.stderr)}",
            "Check that this ffmpeg build provides sidechaincompress, amix and "
            "alimiter; retry with fallback enabled for a plain mix.",
        )

    plain_filter = (
        "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0,"
        "alimiter=limit=0.97[a]"
    )
    result = _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(bed),
            "-i",
            str(voice),
            "-filter_complex",
            plain_filter,
            "-map",
            "[a]",
            "-ac",
            "2",
            "-ar",
            str(SAMPLE_RATE),
            "-sample_fmt",
            "s16",
            str(destination),
        ],
        timeout=600.0,
    )
    if result.returncode != 0:
        raise AgentVoiceError(
            ErrorCode.INTERNAL_ERROR,
            f"bed/voice mix failed: {_ffmpeg_error_tail(result.stderr)}",
            "Check that this ffmpeg build provides amix and alimiter.",
        )
    return destination, False


def _escape_filter_path(path: Path) -> str:
    """Escape a path for use as an ffmpeg filter *option value*.

    A filtergraph has two parsers between the argument and the filename: the graph
    parser (``, ; [ ]`` delimit filters and pads) and the option parser (``:``
    separates options, ``'`` quotes). A path containing any of them — a Windows
    drive letter, a title with a comma — would otherwise split the graph and make
    ffmpeg fail with a message about a filter that was never asked for. The
    backslash must be escaped first so the escapes added below survive.
    """
    text = str(Path(path).expanduser().resolve())
    for char in ("\\", ":", "'", ",", "[", "]"):
        text = text.replace(char, "\\" + char)
    return text


def mux_video(
    media: Path,
    audio: Path,
    destination: Path,
    *,
    srt: Path | None = None,
    burn_subtitles: bool = False,
) -> bool:
    """Put ``audio`` into ``media``'s container, returning whether video was copied.

    The non-burn path remuxes the video stream untouched (``-c:v copy``): a dub
    changes the audio, and re-encoding picture nobody asked to change is a quality
    loss and a large CPU bill for no benefit.

    The burn path re-encodes, which is unavoidable when subtitles become pixels,
    and it fails loudly: a caller who asked for burned-in subtitles and silently
    got a clean video would ship a wrong deliverable. ``+faststart`` is set on both
    so the result streams from the first byte instead of waiting for a full
    download.

    The video map is ``0:v:0?`` (optional) on purpose. Dubbing accepts any media
    with an audio stream — a podcast or a phone recording is a legitimate source —
    and a required ``0:v:0`` would fail the whole render at the last step, after
    every line had already been paid for. An audio-only source therefore yields an
    audio-only MP4, which is the honest deliverable, and the return value still
    reports whether a video stream was actually copied.
    """
    _require_ffmpeg()
    destination.parent.mkdir(parents=True, exist_ok=True)
    has_video = probe_media(media).has_video

    if burn_subtitles:
        if srt is None:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                "burn_subtitles was requested but no subtitle file was produced",
                "Provide segment text (subtitles are built from the translated "
                "lines), or set burn_subtitles=false to deliver a separate .srt.",
            )
        if not has_video:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                "burn_subtitles needs a video stream, and the source has none",
                "Set burn_subtitles=false: the .srt/.vtt sidecar carries the same "
                "cues for an audio-only dub.",
            )
        argv = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(media),
            "-i",
            str(audio),
            "-map",
            "0:v:0?",
            "-map",
            "1:a:0",
            "-vf",
            f"subtitles={_escape_filter_path(srt)}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(destination),
        ]
    else:
        argv = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(media),
            "-i",
            str(audio),
            "-map",
            "0:v:0?",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(destination),
        ]

    result = _run(argv, timeout=900.0)
    if result.returncode != 0:
        raise AgentVoiceError(
            ErrorCode.INTERNAL_ERROR,
            f"could not mux the dub into {Path(destination).name}: "
            f"{_ffmpeg_error_tail(result.stderr)}",
            "Check that the source media is decodable and that this ffmpeg build "
            "provides the aac encoder"
            + (" and the subtitles filter." if burn_subtitles else "."),
        )
    return has_video and not burn_subtitles
