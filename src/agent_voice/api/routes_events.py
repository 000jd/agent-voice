"""Server-sent events for jobs, history and worker state."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter(tags=["events"])

HEARTBEAT_SECONDS = 15.0


@router.get("/v1/events")
async def events(request: Request):
    ctx = request.app.state.ctx
    last_event_id = request.headers.get("last-event-id")
    queue = ctx.events.subscribe(last_event_id)

    async def stream():
        try:
            yield "retry: 5000\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": heartbeat\n\n"
                    continue
                payload = json.dumps(item.data, separators=(",", ":"), default=str)
                yield f"id: {item.id}\nevent: {item.event}\ndata: {payload}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            ctx.events.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
