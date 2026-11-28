"""Safe ingestion of caller-supplied media.

No byte in this module is trusted because of its filename, MIME type or container
metadata. The first 16 KiB are sniffed, the demuxer is forced from a closed
table, and ffmpeg/ffprobe are invoked with a protocol/format whitelist and hard
resource caps. A crafted playlist, concat script or MP4 external reference must
fail before it can read another file or open a socket.

The canonical output is 24 kHz mono 16-bit WAV, the same format the voice store
already uses. Quality/peaks are measured on that trusted local file, never by
trusting the caller's claims.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .schemas import AgentVoiceError, ClippingReport, DraftQuality, ErrorCode, QualityWarning

SAMPLE_RATE = 24_000
READ_HEADER_BYTES = 16 * 1024
MAX_ID3_BYTES = 1024 * 1024
UPLOAD_CHUNK = 1024 * 1024

ALLOWED_FORMATS: tuple[str, ...] = (
    "wav",
    "flac",
    "ogg",
    "matroska",
    "mov",
    "mp3",
    "aac",
    "mpegts",
)
"""Demuxers the daemon will let libavformat open. Deliberately no hls/concat/dash/
image2/subfile/vapoursynth/lavfi."""

EXTENSION_BY_DEMUXER: dict[str, str] = {
    "wav": "wav",
    "flac": "flac",
    "ogg": "ogg",
    "matroska": "mkv",
    "mov": "mp4",
    "mp3": "mp3",
    "aac": "aac",
    "mpegts": "ts",
}

PLAYLIST_MARKERS = (
    b"#extm3u",
    b"ffconcat version",
    b"[playlist]",
    b"webvtt",
)
"""Content that must never be handed to ffmpeg as media, even if a demuxer could
be tricked into accepting it."""


@dataclass(frozen=True)
class IngestInfo:
    path: Path
    demuxer: str
    duration_sec: float
    has_audio: bool
    has_video: bool
    sample_rate: int
    channels: int
    stream_count: int


class IngestError(AgentVoiceError):
    pass


def _error(code: ErrorCode, message: str, suggestion: str) -> AgentVoiceError:
    return AgentVoiceError(code, message, suggestion)


def sniff_demuxer(path: Path) -> str:
    """Return a demuxer from the closed table, or raise 415.

    A RIFF/WAVE header followed by arbitrary bytes is still routed to the WAV
    demuxer; the protocol/format/resource caps are what make the decoder safe.
    The sniff is a routing decision, not an authorization.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(READ_HEADER_BYTES)
    except OSError as exc:
        raise _error(
            ErrorCode.BAD_REQUEST,
            f"cannot read media: {Path(path).name}",
            "Check the file exists and is readable by the daemon.",
        ) from exc
    if not header:
        raise _error(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            "empty media file",
            "Upload a non-empty audio or video file.",
        )

    lowered = header[:4096].lower()
    if any(marker in lowered for marker in PLAYLIST_MARKERS):
        raise _error(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            "playlist, concat script and subtitle containers are not accepted",
            "Upload the audio itself (WAV, FLAC, Ogg, MP3, AAC, MP4, WebM or MKV).",
        )
    if header.startswith((b"PK\x03\x04", b"\x1f\x8b", b"%PDF", b"\x7fELF", b"MZ")):
        raise _error(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            "the uploaded file is not an audio/video container",
            "Upload the media itself, not an archive, image, document or executable.",
        )
    if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return "wav"
    if header[:4] == b"RIFF" and header[8:12] == b"AVI ":
        raise _error(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            "AVI is not in the accepted media set",
            "Convert to MP4/MKV/WebM or upload an audio-only file.",
        )
    if header[:4] == b"fLaC":
        return "flac"
    if header[:4] == b"OggS":
        return "ogg"
    if header[:4] == b"\x1aE\xdf\xa3":
        return "matroska"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "mov"
    if header[:3] == b"ID3":
        return "mp3"
    if len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xF6) == 0xF0:
        return "aac"
    if len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
        return "mp3"
    if any(len(header) > offset and header[offset] == 0x47 for offset in (0, 188, 376)):
        return "mpegts"
    raise _error(
        ErrorCode.UNSUPPORTED_MEDIA_TYPE,
        "unsupported media signature",
        "Use WAV, FLAC, Ogg/Opus, MP3, AAC, MP4/M4A, WebM or MKV.",
    )


def _safe_env() -> dict[str, str]:
    path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    return {
        "PATH": path,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _run_media(
    argv: list[str],
    *,
    timeout: float,
    stdout_pipe: bool = True,
    stderr_limit: int = 64 * 1024,
) -> tuple[int, bytes, bytes]:
    """Run an argv list with hard bounds; never a shell string.

    stderr is captured to a temporary file and only its tail is read, so a
    hostile demuxer cannot turn a failure into unbounded daemon memory.
    """
    with tempfile.TemporaryFile() as err_file:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if stdout_pipe else subprocess.DEVNULL,
            stderr=err_file,
            shell=False,
            start_new_session=True,
            close_fds=True,
            env=_safe_env(),
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    pass
            proc.wait()
            raise AgentVoiceError(
                ErrorCode.TIMEOUT,
                f"media processing exceeded {timeout:.0f}s",
                "Use a shorter or simpler file; the daemon enforces a hard timeout.",
            ) from None
        err_file.seek(0, os.SEEK_END)
        size = err_file.tell()
        err_file.seek(max(0, size - stderr_limit))
        stderr = err_file.read()
        return proc.returncode, stdout or b"", stderr


def input_options(demuxer: str, max_seconds: float, max_streams: int = 8) -> list[str]:
    """Public form of the forced-demuxer input options for audio_utils callers."""
    return _input_args(demuxer, max_seconds, max_streams)


def _probe_input_args(demuxer: str, max_streams: int) -> list[str]:
    return [
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        ",".join(ALLOWED_FORMATS),
        "-f",
        demuxer,
        "-probesize",
        "5000000",
        "-analyzeduration",
        "5000000",
        "-max_streams",
        str(max_streams),
    ]


def _input_args(demuxer: str, max_seconds: float, max_streams: int) -> list[str]:
    return [
        *_probe_input_args(demuxer, max_streams),
        "-t",
        f"{max(0.001, max_seconds):.3f}",
    ]


def _tail(stderr: bytes) -> str:
    text = stderr.decode("utf-8", "replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-3:])[:250] or "ffmpeg reported no detail"


def probe_untrusted(
    path: Path,
    *,
    max_seconds: float,
    max_streams: int = 8,
    max_channels: int = 8,
    max_sample_rate: int = 192_000,
    timeout: float = 5.0,
    require_audio: bool = True,
) -> IngestInfo:
    """ffprobe with a forced demuxer and bounded output."""
    demuxer = sniff_demuxer(path)
    argv = [
        "ffprobe",
        "-hide_banner",
        "-v",
        "error",
        *_probe_input_args(demuxer, max_streams),
        "-show_entries",
        "format=duration:stream=index,codec_type,sample_rate,channels,width,height,duration"
        ":stream_disposition=attached_pic",
        "-of",
        "json",
        "-i",
        str(path),
    ]
    rc, stdout, stderr = _run_media(argv, timeout=timeout)
    if rc != 0:
        raise AgentVoiceError(
            ErrorCode.INVALID_REF_AUDIO,
            f"could not probe {Path(path).name}: {_tail(stderr)}",
            "Provide a file ffmpeg can decode; install a current ffmpeg if it cannot.",
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AgentVoiceError(
            ErrorCode.INVALID_REF_AUDIO,
            f"could not parse ffprobe output for {Path(path).name}",
            "Re-export the file; a truncated container is the usual cause.",
        ) from exc

    streams = payload.get("streams") or []
    if len(streams) > max_streams:
        raise AgentVoiceError(
            ErrorCode.MEDIA_LIMIT_EXCEEDED,
            f"media has {len(streams)} streams; limit is {max_streams}",
            "Remux a single audio stream and retry.",
        )
    audio = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "audio"
            and not (stream.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if audio is None and require_audio:
        raise AgentVoiceError(
            ErrorCode.INVALID_REF_AUDIO,
            f"no audio stream in {Path(path).name}",
            "Provide a file with an audio track.",
        )

    def _int(value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    sample_rate = _int((audio or {}).get("sample_rate"))
    channels = _int((audio or {}).get("channels"))
    if channels > max_channels:
        raise AgentVoiceError(
            ErrorCode.MEDIA_LIMIT_EXCEEDED,
            f"audio has {channels} channels; limit is {max_channels}",
            "Downmix to mono or stereo before uploading.",
        )
    if sample_rate > max_sample_rate:
        raise AgentVoiceError(
            ErrorCode.MEDIA_LIMIT_EXCEEDED,
            f"audio sample rate is {sample_rate} Hz; limit is {max_sample_rate}",
            "Resample to 48 kHz or lower before uploading.",
        )
    duration = 0.0
    try:
        duration = float((payload.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        for stream in streams:
            try:
                duration = max(duration, float(stream.get("duration") or 0.0))
            except (TypeError, ValueError):
                continue
    if duration > max_seconds + 0.05:
        raise AgentVoiceError(
            ErrorCode.MEDIA_TOO_LONG,
            f"media is {duration:.1f}s; limit is {max_seconds:.0f}s",
            "Trim the clip before uploading.",
        )
    return IngestInfo(
        path=Path(path),
        demuxer=demuxer,
        duration_sec=duration,
        has_audio=audio is not None,
        has_video=video is not None,
        sample_rate=sample_rate,
        channels=channels,
        stream_count=len(streams),
    )


def extract_wav_untrusted(
    source: Path,
    destination: Path,
    *,
    demuxer: str,
    max_seconds: float,
    max_decoded_bytes: int,
    sample_rate: int = SAMPLE_RATE,
    max_streams: int = 8,
    timeout: float = 600.0,
) -> Path:
    """Decode one audio stream to canonical mono WAV with input/output caps."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        *_input_args(demuxer, max_seconds, max_streams),
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-sample_fmt",
        "s16",
        "-threads",
        "1",
        "-fs",
        str(max_decoded_bytes),
        "-f",
        "wav",
        "-y",
        str(destination),
    ]
    rc, _, stderr = _run_media(argv, timeout=timeout, stdout_pipe=False)
    if rc != 0:
        raise AgentVoiceError(
            ErrorCode.INVALID_REF_AUDIO,
            f"could not decode {Path(source).name}: {_tail(stderr)}",
            "Provide a decodable audio or video file with an audio track.",
        )
    if destination.stat().st_size > max_decoded_bytes + 1024:
        destination.unlink(missing_ok=True)
        raise AgentVoiceError(
            ErrorCode.MEDIA_TOO_LARGE,
            "decoded audio exceeded the configured byte cap",
            "Use a shorter clip.",
        )
    return destination


def slice_untrusted(
    source: Path,
    destination: Path,
    *,
    start_sec: float,
    duration_sec: float,
    demuxer: str,
    max_seconds: float,
    max_decoded_bytes: int,
    timeout: float = 120.0,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        ",".join(ALLOWED_FORMATS),
        "-f",
        demuxer,
        "-ss",
        f"{max(0.0, start_sec):.3f}",
        "-t",
        f"{max(0.001, duration_sec):.3f}",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-sample_fmt",
        "s16",
        "-threads",
        "1",
        "-fs",
        str(max_decoded_bytes),
        "-f",
        "wav",
        "-y",
        str(destination),
    ]
    rc, _, stderr = _run_media(argv, timeout=timeout, stdout_pipe=False)
    if rc != 0:
        raise AgentVoiceError(
            ErrorCode.INVALID_REF_AUDIO,
            f"could not trim the reference clip: {_tail(stderr)}",
            "Choose a start/end inside the recording and retry.",
        )
    return destination


def _sniff_extension(demuxer: str) -> str:
    return EXTENSION_BY_DEMUXER.get(demuxer, "bin")


async def persist_upload(upload: Any, destination: Path, *, max_bytes: int) -> int:
    """Stream an UploadFile to a private staging file, counting bytes."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    total = 0
    try:
        with os.fdopen(descriptor, "wb") as handle:
            while chunk := await upload.read(UPLOAD_CHUNK):
                total += len(chunk)
                if total > max_bytes:
                    raise AgentVoiceError(
                        ErrorCode.MEDIA_TOO_LARGE,
                        f"upload exceeds the {max_bytes // (1024 * 1024)} MiB limit",
                        "Trim or compress the recording before uploading.",
                    )
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if total == 0:
        destination.unlink(missing_ok=True)
        raise AgentVoiceError(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            "empty upload",
            "Choose a non-empty audio or video file.",
        )
    return total


def stage_upload_name(directory: Path, demuxer: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=directory,
        prefix=".upload-",
        suffix=f".{_sniff_extension(demuxer)}",
        delete=False,
    ) as handle:
        return Path(handle.name)


def compute_peaks(path: Path, *, bins: int = 240) -> list[float]:
    import soundfile as sf

    data, _ = sf.read(path, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if data.size == 0:
        return []
    bucket = max(1, math.ceil(len(data) / bins))
    peaks: list[float] = []
    for start in range(0, len(data), bucket):
        chunk = data[start : start + bucket]
        peaks.append(float(np.max(np.abs(chunk))) if chunk.size else 0.0)
    loudest = max(peaks, default=0.0) or 1.0
    return [round(max(0.02, min(1.0, value / loudest)), 4) for value in peaks[:bins]]


def _ffmpeg_loudness(path: Path) -> tuple[float | None, float | None]:
    """Integrated LUFS and true peak from ffmpeg's loudnorm measurement.

    This is a second decode of an already-canonical, short WAV. Failure is never
    fatal: the signal report still has peak/RMS/clipping from soundfile.
    """
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        "-i",
        str(path),
        "-af",
        "loudnorm=I=-16:TP=-1.5:print_format=json",
        "-f",
        "null",
        "-",
    ]
    try:
        rc, _, stderr = _run_media(argv, timeout=60.0, stdout_pipe=False)
    except AgentVoiceError:
        return None, None
    if rc != 0:
        return None, None
    text = stderr.decode("utf-8", "replace")
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", text, re.DOTALL)
    if not match:
        return None, None
    try:
        payload = json.loads(match.group(0))
        lufs = float(payload["input_i"])
        peak = float(payload["input_tp"])
        return (
            None if not math.isfinite(lufs) else lufs,
            None if not math.isfinite(peak) else peak,
        )
    except (ValueError, KeyError, TypeError):
        return None, None


def analyse_quality(path: Path, *, max_duration: float = 30.0) -> DraftQuality:
    import soundfile as sf

    data, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if data.size == 0:
        raise AgentVoiceError(
            ErrorCode.INVALID_REF_AUDIO,
            "the decoded recording is empty",
            "Record at least one second of speech.",
        )
    duration = len(data) / sample_rate
    peak = float(np.max(np.abs(data)))
    rms = float(np.sqrt(np.mean(np.square(data, dtype=np.float64))))
    peak_dbfs = 20.0 * math.log10(peak) if peak > 0 else -120.0
    rms_dbfs = 20.0 * math.log10(rms) if rms > 0 else -120.0
    silence_ratio = float(np.mean(np.abs(data) < 10 ** (-50 / 20)))
    clipped = np.abs(data) >= 0.999
    clipped_count = int(np.count_nonzero(clipped))
    first_at = float(np.argmax(clipped) / sample_rate) if clipped_count else None
    integrated, true_peak = _ffmpeg_loudness(path)

    warnings: list[QualityWarning] = []
    if duration < 1.0:
        warnings.append(
            QualityWarning(
                code="too_short",
                severity="error",
                message=f"clip is {duration:.2f}s; at least 1s is required",
                suggestion="Record or trim to a continuous phrase of at least 3 seconds.",
            )
        )
    elif duration < 3.0:
        warnings.append(
            QualityWarning(
                code="short",
                severity="warning",
                message=f"clip is {duration:.2f}s; 3-10s is the quality sweet spot",
                suggestion="Add another second or two of continuous speech.",
            )
        )
    elif duration > max_duration:
        warnings.append(
            QualityWarning(
                code="too_long",
                severity="error",
                message=f"clip is {duration:.2f}s; the reference limit is {max_duration:.0f}s",
                suggestion="Trim to a focused 3-10s phrase.",
            )
        )
    if clipped_count:
        warnings.append(
            QualityWarning(
                code="clipped",
                severity="warning",
                message=f"{clipped_count} samples reached full scale",
                suggestion=(
                    "Lower the input gain and record again; clipped audio cannot be recovered."
                ),
            )
        )
    if silence_ratio > 0.5:
        warnings.append(
            QualityWarning(
                code="silent",
                severity="warning",
                message=f"{silence_ratio:.0%} of the clip is below -50 dBFS",
                suggestion="Trim silence or re-record closer to the microphone.",
            )
        )
    if integrated is not None and integrated < -35:
        warnings.append(
            QualityWarning(
                code="too_quiet",
                severity="warning",
                message=f"integrated loudness is {integrated:.1f} LUFS",
                suggestion="Move closer to the microphone or raise the input level.",
            )
        )
    return DraftQuality(
        duration_sec=round(duration, 3),
        peak_dbfs=round(peak_dbfs, 2),
        rms_dbfs=round(rms_dbfs, 2),
        true_peak_dbtp=round(true_peak, 2) if true_peak is not None else None,
        integrated_lufs=round(integrated, 2) if integrated is not None else None,
        silence_ratio=round(silence_ratio, 4),
        clipping=ClippingReport(
            detected=bool(clipped_count),
            clipped_samples=clipped_count,
            first_at_sec=round(first_at, 3) if first_at is not None else None,
        ),
        warnings=warnings,
    )


def assert_ffmpeg_capabilities() -> None:
    """Fail loudly if the installed ffmpeg cannot enforce the ingest boundary."""
    for binary in ("ffmpeg", "ffprobe"):
        if shutil.which(binary) is None:
            raise AgentVoiceError(
                ErrorCode.INTERNAL_ERROR,
                f"{binary} is not on PATH",
                "Install ffmpeg and ffprobe; media ingest cannot run without them.",
            )
    try:
        rc, stdout, _ = _run_media(
            ["ffmpeg", "-hide_banner", "-h", "full"],
            timeout=20.0,
        )
    except AgentVoiceError:
        raise
    text = stdout.decode("utf-8", "replace")
    missing = [
        flag
        for flag in ("-format_whitelist", "-protocol_whitelist", "-max_streams")
        if flag not in text
    ]
    if rc != 0 or missing:
        raise AgentVoiceError(
            ErrorCode.INTERNAL_ERROR,
            f"this ffmpeg lacks required ingest safeguards: {', '.join(missing) or 'help failed'}",
            "Install a newer ffmpeg; the daemon refuses to auto-demux untrusted media.",
        )


def archive_member_is_safe(name: str) -> bool:
    """Reject archive names that could escape a fresh extraction root."""
    if not name or "\x00" in name or "\\" in name:
        return False
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return False
    parts = [part for part in name.split("/") if part not in {"", "."}]
    return bool(parts) and all(part != ".." for part in parts)


def safe_zip_extract(
    source: Path,
    destination: Path,
    *,
    max_entries: int = 256,
    max_entry_bytes: int = 64 * 1024**2,
    max_total_bytes: int = 256 * 1024**2,
    max_ratio: int = 100,
) -> list[Path]:
    """Extract a voice bundle with zip-slip, symlink and bomb protections."""
    import zipfile

    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total = 0
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if len(infos) > max_entries:
                raise AgentVoiceError(
                    ErrorCode.ARCHIVE_TOO_LARGE,
                    f"archive has {len(infos)} entries; limit is {max_entries}",
                    "Export a smaller voice bundle.",
                )
            for info in infos:
                if info.is_dir():
                    continue
                if not archive_member_is_safe(info.filename):
                    raise AgentVoiceError(
                        ErrorCode.ARCHIVE_UNSAFE,
                        f"unsafe archive member: {info.filename!r}",
                        (
                            "The bundle may not contain absolute paths, traversal, "
                            "symlinks or backslashes."
                        ),
                    )
                if info.flag_bits & 0x1:
                    raise AgentVoiceError(
                        ErrorCode.ARCHIVE_UNSAFE,
                        "encrypted archives are not accepted",
                        "Export the voice without a password.",
                    )
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise AgentVoiceError(
                        ErrorCode.ARCHIVE_UNSAFE,
                        f"symlink archive member: {info.filename!r}",
                        "The bundle must contain regular files only.",
                    )
                folded = info.filename.casefold()
                if folded in seen:
                    raise AgentVoiceError(
                        ErrorCode.ARCHIVE_UNSAFE,
                        f"duplicate archive member: {info.filename!r}",
                        "Remove duplicate or case-colliding entries.",
                    )
                seen.add(folded)
                target = destination / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with archive.open(info) as reader, open(target, "wb") as writer:
                    while chunk := reader.read(1024 * 1024):
                        written += len(chunk)
                        total += len(chunk)
                        if written > max_entry_bytes or total > max_total_bytes:
                            raise AgentVoiceError(
                                ErrorCode.ARCHIVE_TOO_LARGE,
                                "archive expands beyond the configured limits",
                                "Export a smaller voice bundle.",
                            )
                        writer.write(chunk)
                if info.file_size and written > info.file_size * max_ratio:
                    raise AgentVoiceError(
                        ErrorCode.ARCHIVE_TOO_LARGE,
                        "archive compression ratio is unsafe",
                        "Export the voice without extreme compression.",
                    )
                extracted.append(target)
    except zipfile.BadZipFile as exc:
        raise AgentVoiceError(
            ErrorCode.ARCHIVE_UNSAFE,
            "not a valid zip bundle",
            "Export a voice bundle from this daemon and retry.",
        ) from exc
    return extracted


async def disk_has_room(path: Path, min_free_bytes: int) -> bool:
    await asyncio.sleep(0)
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return False
    return free >= min_free_bytes


def extension_for(path: Path) -> str:
    return _sniff_extension(sniff_demuxer(path))
