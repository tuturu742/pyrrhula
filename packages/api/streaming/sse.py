"""SSE stream endpoint support (T0.8). ``Last-Event-ID`` resume: on connect, replay
every ``session_event`` with ``event_seq`` greater than the client's last-seen id
straight from the database (durable — this is what makes reconnecting after a missed
message work), *then* subscribe to the live Redis channel for anything new. A client
that disconnects mid-*token-stream* (not mid-message) will miss the in-progress partial
text on reconnect — the durable log is message-granular, not token-granular; it will
still see the completed message once it lands, live if still connected or via catch-up
on the next reconnect.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import select

from api.streaming.pubsub import subscribe
from core.sessions.models import SessionEventRow
from core.tenancy.scope import tenant_scope


def _format_sse(data: str, *, event_id: int | None = None, event: str | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if event is not None:
        lines.append(f"event: {event}")
    lines.append(f"data: {data}")
    return "\n".join(lines) + "\n\n"


async def sse_stream(
    tenant_id: uuid.UUID, session_id: uuid.UUID, request: Request
) -> AsyncIterator[str]:
    last_event_id_header = request.headers.get("last-event-id")
    last_seq = int(last_event_id_header) if last_event_id_header else -1

    async with tenant_scope(tenant_id) as session:
        catchup_rows = (
            (
                await session.execute(
                    select(SessionEventRow)
                    .where(
                        SessionEventRow.session_id == session_id,
                        SessionEventRow.event_seq > last_seq,
                    )
                    .order_by(SessionEventRow.event_seq)
                )
            )
            .scalars()
            .all()
        )

    for row in catchup_rows:
        yield _format_sse(
            json.dumps({"kind": row.kind, "payload": row.payload}),
            event_id=row.event_seq,
            event="message",
        )

    async for msg in subscribe(session_id):
        if await request.is_disconnected():
            break
        if msg is None:
            continue
        if msg["type"] == "chunk":
            yield _format_sse(json.dumps({"text": msg["text"]}), event="chunk")
        else:
            yield _format_sse(
                json.dumps({"kind": msg["kind"], "payload": msg["payload"]}),
                # ephemeral events (typing cues, seq < 0) carry no id: they are not
                # durable, must not advance Last-Event-ID, and skip the client's
                # seq-dedupe guard
                event_id=msg["event_seq"] if msg["event_seq"] >= 0 else None,
                event="message",
            )
