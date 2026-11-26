"""Frozen IPC transport, version 1.

UDS on POSIX, loopback TCP on Windows. One ``Transport`` ABC, two listeners,
plus an async client used by ``worker_manager``.

Design notes:
* The worker side is intentionally **synchronous**: a dedicated reader thread
  must keep draining the socket so ``cancel`` arrives while the GPU thread is
  buried inside a decode loop.
* The parent side is **asyncio**: one reader task demultiplexes JSON replies by
  ``id`` and PCM chunks by ``stream_id``, each with a bounded queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..schemas import AgentVoiceError, ErrorCode
from .protocol import (
    FRAME_JSON,
    FRAME_PCM,
    LENGTH_PREFIX_SIZE,
    decode_header,
    encode_frame,
    encode_json,
    split_pcm_payload,
)

IS_WINDOWS = sys.platform == "win32"


class Transport(ABC):
    """A connectable IPC endpoint. Address differs per platform."""

    @abstractmethod
    def address(self) -> str:
        """Human-readable address for logs and the worker handshake."""

    @abstractmethod
    def to_env(self) -> dict[str, str]:
        """Environment variables passed to the spawned worker."""


class UnixTransport(Transport):
    """Unix domain socket in the engine's runtime directory."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def address(self) -> str:
        return f"unix://{self.path}"

    def to_env(self) -> dict[str, str]:
        return {"AGENT_VOICE_IPC_TRANSPORT": "unix", "AGENT_VOICE_IPC_PATH": str(self.path)}


class TcpTransport(Transport):
    """Loopback TCP with an ephemeral port (Windows has no usable UDS)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self.port = port

    def address(self) -> str:
        return f"tcp://{self.host}:{self.port}"

    def to_env(self) -> dict[str, str]:
        return {
            "AGENT_VOICE_IPC_TRANSPORT": "tcp",
            "AGENT_VOICE_IPC_HOST": self.host,
            "AGENT_VOICE_IPC_PORT": str(self.port),
        }


def make_transport(path: Path) -> Transport:
    return TcpTransport() if IS_WINDOWS else UnixTransport(path)


def transport_from_env(env: dict[str, str]) -> Transport:
    """Reconstruct the transport inside a worker from its environment."""
    kind = env.get("AGENT_VOICE_IPC_TRANSPORT", "unix")
    if kind == "tcp":
        return TcpTransport(env["AGENT_VOICE_IPC_HOST"], int(env["AGENT_VOICE_IPC_PORT"]))
    if kind == "unix":
        return UnixTransport(Path(env["AGENT_VOICE_IPC_PATH"]))
    raise ValueError(f"unknown IPC transport: {kind!r}")


# ---------------------------------------------------------------------------
# Synchronous side (worker process)
# ---------------------------------------------------------------------------


class SyncConnection:
    """Blocking framed connection. Used by workers; thread-safe writes."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._write_lock = threading.Lock()

    # -- reading --------------------------------------------------------
    def recv_frame(self) -> tuple[int, bytes]:
        """Read one whole frame. Returns (frame_type, payload)."""
        header = self._recv_exact(LENGTH_PREFIX_SIZE + 1)
        frame_type, payload_len = decode_header(header)
        payload = self._recv_exact(payload_len) if payload_len else b""
        return frame_type, payload

    def _recv_exact(self, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise ConnectionError("IPC peer closed the connection")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    # -- writing --------------------------------------------------------
    def send_json(self, obj: dict[str, Any]) -> None:
        payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self._send(encode_json(payload))

    def send_pcm(self, stream_id: uuid.UUID, pcm: bytes) -> None:
        self._send(encode_frame(FRAME_PCM, stream_id.bytes + pcm))

    def _send(self, frame: bytes) -> None:
        with self._write_lock:
            self._sock.sendall(frame)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class SyncListener:
    """Blocking listener. The worker accepts one parent connection."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self._sock: socket.socket | None = None

    def bind(self) -> None:
        if isinstance(self.transport, UnixTransport):
            path = self.transport.path
            path.parent.mkdir(parents=True, exist_ok=True)
            # The kernel caps sun_path at 108 bytes; a deeper data dir makes this
            # fail with a bare OSError that gives no hint about the real cause.
            encoded_length = len(os.fsencode(str(path)))
            if encoded_length > 107:
                raise AgentVoiceError(
                    ErrorCode.INTERNAL_ERROR,
                    f"IPC socket path is {encoded_length} bytes, "
                    f"but the kernel limit is 107: {path}",
                    "Set DATA_DIR to a shorter path, or let the manager fall back "
                    "to the system temp directory.",
                )
            if path.exists():
                path.unlink()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.bind(str(path))
            except OSError as exc:
                sock.close()
                raise AgentVoiceError(
                    ErrorCode.INTERNAL_ERROR,
                    f"could not bind IPC socket at {path}: {exc}",
                    "Check that DATA_DIR is writable and the path is not too long.",
                ) from exc
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.transport.host, self.transport.port))
            self.transport.port = sock.getsockname()[1]
        sock.listen(1)
        self._sock = sock

    def accept_socket(self) -> socket.socket:
        """Blocking accept returning the raw socket (used by async adapter)."""
        assert self._sock is not None, "bind() before accept()"
        conn, _ = self._sock.accept()
        if not isinstance(self.transport, UnixTransport):
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return conn

    def accept(self) -> SyncConnection:
        return SyncConnection(self.accept_socket())

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if isinstance(self.transport, UnixTransport):
            try:
                self.transport.path.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Asynchronous side (parent daemon)
# ---------------------------------------------------------------------------


class AsyncConnection:
    """Asyncio framed connection with id/stream demultiplexing.

    * JSON replies resolve the ``Future`` registered for their ``id``.
    * PCM chunks are pushed into the bounded queue for their ``stream_id``.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        stream_queue_factory: Callable[[], asyncio.Queue[bytes]] | None = None,
        on_pcm: Callable[[uuid.UUID, bytes], None] | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._streams: dict[uuid.UUID, asyncio.Queue[bytes]] = {}
        self._queue_factory = stream_queue_factory or (lambda: asyncio.Queue(maxsize=32))
        self._on_pcm = on_pcm
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()

    # -- lifecycle ------------------------------------------------------
    async def open(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop(), name="ipc-reader")

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def close(self) -> None:
        self._closed.set()
        if self._reader_task is not None:
            self._reader_task.cancel()
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except (OSError, ConnectionError):
            pass
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("IPC connection closed"))
        self._pending.clear()
        self._streams.clear()

    # -- reading --------------------------------------------------------
    async def _read_loop(self) -> None:
        try:
            while not self._closed.is_set():
                header = await self._reader.readexactly(LENGTH_PREFIX_SIZE + 1)
                frame_type, payload_len = decode_header(header)
                payload = await self._reader.readexactly(payload_len) if payload_len else b""
                if frame_type == FRAME_JSON:
                    self._dispatch_json(payload)
                elif frame_type == FRAME_PCM:
                    self._dispatch_pcm(payload)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self._closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("IPC connection lost"))
            self._pending.clear()

    def _dispatch_json(self, payload: bytes) -> None:
        message = json.loads(payload.decode("utf-8"))
        request_id = message.get("id")
        future = self._pending.pop(request_id, None) if request_id else None
        if future is not None and not future.done():
            future.set_result(message)

    def _dispatch_pcm(self, payload: bytes) -> None:
        stream_id, pcm = split_pcm_payload(payload)
        if self._on_pcm is not None:
            self._on_pcm(stream_id, pcm)
            return
        queue = self._streams.get(stream_id)
        if queue is None:
            return
        try:
            queue.put_nowait(pcm)
        except asyncio.QueueFull:
            # Caller is responsible for turning this into BACKPRESSURE.
            pass

    # -- writing --------------------------------------------------------
    def _send(self, frame: bytes) -> None:
        if self._closed.is_set():
            raise ConnectionError("IPC connection is closed")
        self._writer.write(frame)

    async def _drain(self) -> None:
        """Flush the write buffer.

        Without this, a long PCM stream fills asyncio's transport buffer, the
        socket stops draining, and the peer's replies stop arriving — which shows
        up as a mid-stream `health` call hanging forever.
        """
        with contextlib.suppress(ConnectionError, OSError):
            await self._writer.drain()

    async def request(self, obj: dict[str, Any], *, timeout: float = 60.0) -> dict[str, Any]:
        request_id = obj["id"]
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._send(encode_json(json.dumps(obj, separators=(",", ":")).encode("utf-8")))
        await self._drain()
        try:
            # shield() so a caller timeout cancels only the wait, never the
            # shared future that the reader task will complete later.
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except TimeoutError as exc:
            raise TimeoutError(
                f"IPC request '{obj.get('method')}' timed out after {timeout}s"
            ) from exc
        finally:
            self._pending.pop(request_id, None)

    def send_json_nowait(self, obj: dict[str, Any]) -> None:
        self._send(encode_json(json.dumps(obj, separators=(",", ":")).encode("utf-8")))

    def send_pcm(self, stream_id: uuid.UUID, pcm: bytes) -> None:
        self._send(encode_frame(FRAME_PCM, stream_id.bytes + pcm))

    # -- streams --------------------------------------------------------
    def open_stream(self, stream_id: uuid.UUID) -> asyncio.Queue[bytes]:
        queue = self._queue_factory()
        self._streams[stream_id] = queue
        return queue

    def close_stream(self, stream_id: uuid.UUID) -> None:
        self._streams.pop(stream_id, None)


async def connect_async(transport: Transport, **kwargs: Any) -> AsyncConnection:
    """Dial a listening worker (used when the *worker* listens)."""
    if isinstance(transport, UnixTransport):
        reader, writer = await asyncio.open_unix_connection(str(transport.path))
    else:
        reader, writer = await asyncio.open_connection(transport.host, transport.port)
    conn = AsyncConnection(reader, writer, **kwargs)
    await conn.open()
    return conn


async def accept_async(listener: SyncListener, **kwargs: Any) -> AsyncConnection:
    """Accept the worker's connection and wrap it in asyncio streams.

    This is the mode agent-voice actually uses: the parent binds the socket
    before spawning, so the address is known without a handshake round-trip,
    and the worker dials in. The accept itself is blocking, so it runs in a
    thread to keep the event loop responsive.
    """
    loop = asyncio.get_running_loop()
    sock = await loop.run_in_executor(None, listener.accept_socket)
    sock.setblocking(False)
    reader, writer = await asyncio.open_connection(sock=sock)
    conn = AsyncConnection(reader, writer, **kwargs)
    await conn.open()
    return conn


__all__ = [
    "IS_WINDOWS",
    "AsyncConnection",
    "SyncConnection",
    "SyncListener",
    "TcpTransport",
    "Transport",
    "UnixTransport",
    "accept_async",
    "connect_async",
    "make_transport",
    "transport_from_env",
]
