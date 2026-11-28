"""Small in-process event fan-out for SSE.

Events are intentionally ephemeral. A daemon restart drops the replay ring; the
client refetches jobs/history instead of pretending it can resume old events.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Event:
    id: str
    event: str
    data: dict[str, Any]


class EventBus:
    def __init__(self, *, replay: int = 64, queue_size: int = 128) -> None:
        self._ids = itertools.count(1)
        self._events: deque[Event] = deque(maxlen=replay)
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._queue_size = queue_size

    def publish(self, event: str, data: dict[str, Any]) -> Event:
        item = Event(id=str(next(self._ids)), event=event, data=data)
        self._events.append(item)
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                pass
        return item

    def subscribe(self, last_event_id: str | None = None) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        if last_event_id and last_event_id.isdigit():
            needle = int(last_event_id)
            for item in self._events:
                if int(item.id) > needle:
                    queue.put_nowait(item)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)
