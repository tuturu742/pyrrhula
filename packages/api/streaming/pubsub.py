"""Redis pub/sub fan-out for session streams (T0.8). One channel per session:
``session:{id}``. Two message shapes on the same channel: ephemeral ``chunk`` (live
token deltas — only ever delivered live, never durable, never resumable) and durable
``event`` (mirrors a ``session_event`` row — delivered live here *and* replayable from
the database on reconnect via ``Last-Event-ID``, see ``sse.py``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from api.redis_client import get_redis


def _channel(session_id: uuid.UUID) -> str:
    return f"session:{session_id}"


async def publish_chunk(session_id: uuid.UUID, text: str) -> None:
    await get_redis().publish(_channel(session_id), json.dumps({"type": "chunk", "text": text}))


async def publish_event(
    session_id: uuid.UUID, event_seq: int, kind: str, payload: dict[str, Any]
) -> None:
    await get_redis().publish(
        _channel(session_id),
        json.dumps({"type": "event", "event_seq": event_seq, "kind": kind, "payload": payload}),
    )


async def subscribe(
    session_id: uuid.UUID, *, poll_timeout: float = 1.0
) -> AsyncIterator[dict[str, Any] | None]:
    """Yields ``None`` on each poll timeout with no message, so callers (the SSE route)
    can check for client disconnection periodically instead of blocking indefinitely on
    the next publish."""
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(_channel(session_id))
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=poll_timeout)
            if message is None:
                yield None
                continue
            yield json.loads(message["data"])
    finally:
        await pubsub.unsubscribe(_channel(session_id))
        # redis-py's PubSub.aclose() stub gap, not a real type error.
        await pubsub.aclose()  # type: ignore[no-untyped-call]
