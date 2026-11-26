"""Multiplexed IPC between the daemon and isolated engine workers.

The wire contract lives in :mod:`agent_voice.ipc.protocol` and is frozen.
"""

from .protocol import FRAME_JSON, FRAME_PCM, encode_frame, encode_json, encode_pcm
from .transport import (
    AsyncConnection,
    SyncConnection,
    SyncListener,
    TcpTransport,
    Transport,
    UnixTransport,
    accept_async,
    connect_async,
    make_transport,
    transport_from_env,
)

__all__ = [
    "FRAME_JSON",
    "FRAME_PCM",
    "AsyncConnection",
    "SyncConnection",
    "SyncListener",
    "TcpTransport",
    "Transport",
    "UnixTransport",
    "accept_async",
    "connect_async",
    "encode_frame",
    "encode_json",
    "encode_pcm",
    "make_transport",
    "transport_from_env",
]
