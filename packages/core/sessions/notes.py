"""Transcript notes: append a message to a session outside a live model turn.

Delegated work happens in worker jobs, and its outcomes (a PR opened, a review verdict,
a fix commit) were previously invisible in the session view — recorded in FSM state and
PR records but never in the transcript a human actually reads. A note is an ordinary
``message`` row + ``session_event`` (so SSE and the transcript render it like any turn),
attributed to a real principal (usually the session's supervisor persona).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from core.sessions.lifecycle import resolve_author_name
from core.sessions.models import MessageRow, SessionEventRow, SessionRow
from core.tenancy.scope import tenant_scope


async def post_note(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    author_principal_id: uuid.UUID,
    content_md: str,
) -> int:
    """Append one note to the session transcript. Returns the event_seq it landed on."""
    last_error: IntegrityError | None = None
    for _attempt in range(3):
        try:
            return await _post_note_once(tenant_id, session_id, author_principal_id, content_md)
        except IntegrityError as exc:
            # A concurrent writer (e.g. a delegation action record, which lands on a seq
            # reserved at enqueue time without touching next_event_seq) took the slot --
            # re-derive from the actual max and try again.
            last_error = exc
    raise last_error  # type: ignore[misc]  # three collisions in a row: give up loudly


async def post_system_event(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    kind: str,
    payload: dict[str, Any],
) -> int:
    """Append one system event (no message row, no author) to the transcript -- the
    mechanism behind divider lines like phase transitions, for machinery that runs in
    worker jobs (e.g. exec-environment lifecycle). Same max-seq allocation + retry as
    notes. Returns the event_seq it landed on."""
    last_error: IntegrityError | None = None
    for _attempt in range(3):
        try:
            async with tenant_scope(tenant_id) as session:
                row = await session.get(SessionRow, session_id, with_for_update=True)
                if row is None:
                    raise ValueError(f"no session {session_id} in this tenant")
                max_seq = await session.scalar(
                    select(func.max(SessionEventRow.event_seq)).where(
                        SessionEventRow.session_id == session_id
                    )
                )
                event_seq = max(row.next_event_seq, (max_seq + 1) if max_seq is not None else 0)
                row.next_event_seq = event_seq + 1
                session.add(
                    SessionEventRow(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        event_seq=event_seq,
                        kind=kind,
                        payload=payload,
                    )
                )
                return event_seq
        except IntegrityError as exc:
            last_error = exc
    raise last_error  # type: ignore[misc]  # three collisions in a row: give up loudly


async def _post_note_once(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    author_principal_id: uuid.UUID,
    content_md: str,
) -> int:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id, with_for_update=True)
        if row is None:
            raise ValueError(f"no session {session_id} in this tenant")
        # next_event_seq alone is not the truth: delegation reserves seq ranges up front
        # and its action records land on them later, without bumping the counter again.
        max_seq = await session.scalar(
            select(func.max(SessionEventRow.event_seq)).where(
                SessionEventRow.session_id == session_id
            )
        )
        event_seq = max(row.next_event_seq, (max_seq + 1) if max_seq is not None else 0)
        row.next_event_seq = event_seq + 1

        message = MessageRow(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=event_seq,
            author_principal_id=author_principal_id,
            role="assistant",
            content_md=content_md,
        )
        session.add(message)
        await session.flush()

        author_name = await resolve_author_name(session, tenant_id, author_principal_id)
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                kind="message",
                payload={
                    "id": str(message.id),
                    "role": "assistant",
                    "content": content_md,
                    "tool_calls_made": 0,
                    "author": author_name,
                },
                actor_principal_id=author_principal_id,
            )
        )
        return event_seq
