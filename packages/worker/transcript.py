"""Worker-side transcript writes with live fan-out.

The session SSE stream (api/streaming) replays the database on connect, then listens
on the session's Redis channel for anything new -- but only the api process was ever
publishing to that channel. Events written by worker jobs (delegation notes, review
verdicts, fix notes, exec-environment lines) landed in the database only, so an open
session view did not show the review loop happening until the user reloaded.

These wrappers write through ``core.sessions.notes`` and then publish the stored row
on the same channel contract as ``api.streaming.pubsub`` (channel ``session:{id}``,
message ``{"type": "event", ...}``). Publishing is best-effort: the durable row is the
truth, and a Redis hiccup must never fail the job that wrote it.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from typing import Any

from sqlalchemy import select

from core.config import get_settings
from core.sessions.models import SessionEventRow
from core.sessions.notes import post_note as _post_note
from core.sessions.notes import post_system_event as _post_system_event
from core.tenancy.scope import tenant_scope

_redis_client: Any = None


def _redis() -> Any:
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as redis_async

        _redis_client = redis_async.from_url(get_settings().redis_url)
    return _redis_client


async def _publish_event_row(tenant_id: uuid.UUID, session_id: uuid.UUID, event_seq: int) -> None:
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(SessionEventRow).where(
                SessionEventRow.session_id == session_id,
                SessionEventRow.event_seq == event_seq,
            )
        )
    if row is None:
        return
    await _redis().publish(
        f"session:{session_id}",
        json.dumps(
            {"type": "event", "event_seq": row.event_seq, "kind": row.kind, "payload": row.payload}
        ),
    )


async def post_note(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    author_principal_id: uuid.UUID,
    content_md: str,
) -> int:
    event_seq = await _post_note(tenant_id, session_id, author_principal_id, content_md)
    with contextlib.suppress(Exception):
        await _publish_event_row(tenant_id, session_id, event_seq)
    return event_seq


async def post_system_event(
    tenant_id: uuid.UUID, session_id: uuid.UUID, kind: str, payload: dict[str, Any]
) -> int:
    event_seq = await _post_system_event(tenant_id, session_id, kind, payload)
    with contextlib.suppress(Exception):
        await _publish_event_row(tenant_id, session_id, event_seq)
    return event_seq
