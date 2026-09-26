"""Session lifecycle actions: fetch-by-id, pause, resume. Deliberately thin --
pausing/resuming just marks intent on the session row (the same ``status`` values
``core.process.interpreter``/``core.process.awaits`` already write internally on
fault/await), with no process-engine involvement of its own. A paused session's turn
scheduler simply has nothing driving it forward; resuming doesn't replay or re-derive
anything, it just clears the pause.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.actions.idempotency import clear_failed_operation
from core.sessions.models import SessionEventRow, SessionPersonaRow, SessionRow
from core.tenancy.scope import tenant_scope


async def set_session_roster(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    supervisor_persona_id: uuid.UUID,
    participant_persona_ids: list[uuid.UUID],
) -> None:
    """#4: pin a session's explicit roster -- exactly one supervisor + one or more
    participants. The scheduler then draws a phase's actors from this set only."""
    async with tenant_scope(tenant_id) as session:
        session.add(
            SessionPersonaRow(
                tenant_id=tenant_id,
                session_id=session_id,
                persona_id=supervisor_persona_id,
                is_supervisor=True,
            )
        )
        for pid in participant_persona_ids:
            if pid == supervisor_persona_id:
                continue
            session.add(
                SessionPersonaRow(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    persona_id=pid,
                    is_supervisor=False,
                )
            )
        await session.flush()


async def list_session_roster(
    tenant_id: uuid.UUID, session_id: uuid.UUID
) -> list[SessionPersonaRow]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(SessionPersonaRow).where(SessionPersonaRow.session_id == session_id)
            )
        ).scalars()
        return list(rows)


async def get_session(tenant_id: uuid.UUID, session_id: uuid.UUID) -> SessionRow | None:
    async with tenant_scope(tenant_id) as session:
        return await session.get(SessionRow, session_id)


async def resolve_author_name(
    session: AsyncSession, tenant_id: uuid.UUID, principal_id: uuid.UUID
) -> str:  # noqa: ANN001
    """Display name for the author of a message: the persona's name when the principal is a
    persona, else the human principal's display name. Raw SQL (no model import) so it can run
    inside any caller's transaction without dragging in an import cycle."""
    name = await session.scalar(
        text("SELECT name FROM persona WHERE tenant_id = :t AND principal_id = :p"),
        {"t": tenant_id, "p": principal_id},
    )
    if name:
        return str(name)
    display = await session.scalar(
        text("SELECT display_name FROM principal WHERE id = :p"), {"p": principal_id}
    )
    return display or "Unknown"


async def list_sessions(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, *, include_archived: bool = False
) -> list[SessionRow]:
    """Newest-first sessions in a workspace, for the UI's session list. Archived sessions are
    hidden by default -- ``get_session`` deliberately still returns them by id so an archived
    session's transcript stays viewable and any in-flight resume is unaffected."""
    async with tenant_scope(tenant_id) as session:
        stmt = select(SessionRow).where(
            SessionRow.tenant_id == tenant_id, SessionRow.workspace_id == workspace_id
        )
        if not include_archived:
            stmt = stmt.where(SessionRow.archived_at.is_(None))
        rows = (await session.execute(stmt.order_by(SessionRow.created_at.desc()))).scalars()
        return list(rows)


async def rename_session(
    tenant_id: uuid.UUID, session_id: uuid.UUID, name: str | None
) -> SessionRow:
    """Set (or clear) a session's display name."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            raise ValueError(f"no session {session_id} in this tenant")
        row.name = name.strip()[:255] if name and name.strip() else None
        await session.flush()
        return row


async def last_event_times(
    tenant_id: uuid.UUID, session_ids: list[uuid.UUID]
) -> dict[uuid.UUID, datetime]:
    """Latest ``session_event.created_at`` per session — the list view's "something is
    happening" signal. One grouped query, not one per row."""
    if not session_ids:
        return {}
    async with tenant_scope(tenant_id) as session:
        rows = await session.execute(
            select(SessionEventRow.session_id, func.max(SessionEventRow.created_at))
            .where(SessionEventRow.session_id.in_(session_ids))
            .group_by(SessionEventRow.session_id)
        )
        return {sid: ts for sid, ts in rows}


async def set_session_agenda(
    tenant_id: uuid.UUID, session_id: uuid.UUID, agenda_md: str | None
) -> None:
    """#5: set (or clear) a session's free-form agenda. Injected into the supervisor's turn
    context by core.process.live_session."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            raise ValueError(f"no session {session_id} in this tenant")
        row.agenda_md = agenda_md
        await session.flush()


async def set_session_turn_policy(
    tenant_id: uuid.UUID, session_id: uuid.UUID, turn_policy: str
) -> SessionRow:
    """#7: flip a session between ``'auto'`` (autonomous scheduler) and ``'directed'`` (a
    human conducts each discussion turn). Mutable on purpose -- the overseer toggles it
    mid-run; the interpreter reads it at every scheduling decision."""
    if turn_policy not in ("auto", "directed"):
        raise ValueError(f"invalid turn_policy {turn_policy!r}; expected 'auto' or 'directed'")
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            raise ValueError(f"no session {session_id} in this tenant")
        row.turn_policy = turn_policy
        await session.flush()
        return row


async def set_conductor_wrap_up(tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
    """#7: the overseer ends a conducted discussion. Sets the ``conductor_wrap_up`` state
    var the dual-mode discussion phase's first gate keys on, so the next advance transitions
    to synthesis instead of parking for another conducted turn. A no-op-safe write for a
    definition that doesn't declare the var (it simply won't gate on it)."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            raise ValueError(f"no session {session_id} in this tenant")
        new_state = dict(row.state)
        new_state["conductor_wrap_up"] = True
        row.state = new_state
        await session.flush()


async def reopen_session(
    tenant_id: uuid.UUID, session_id: uuid.UUID, *, target_phase: str, rounds: int
) -> SessionRow:
    """#7 continue: re-open a finished (terminal) flow at ``target_phase`` so it can run more
    turns. The caller (route) picks ``target_phase`` -- the phase flagged ``conductable`` --
    after confirming the current phase is terminal.

    Two state fix-ups make the re-opened phase actually progress rather than immediately fall
    back to synthesis: clear ``conductor_wrap_up`` (else the dual-mode discussion gate jumps
    straight back to synthesis), and, when the definition uses the conventional
    ``round``/``max_rounds`` budget, extend ``max_rounds`` to ``round + rounds`` so the
    autonomous regroup gate grants ``rounds`` more rounds. Both fix-ups are no-ops for a
    definition that doesn't declare those vars -- only keys already present are touched."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            raise ValueError(f"no session {session_id} in this tenant")
        new_state = dict(row.state)
        if "conductor_wrap_up" in new_state:
            new_state["conductor_wrap_up"] = False
        if "round" in new_state and "max_rounds" in new_state:
            # A hand-edited or legacy state value that will not parse leaves max_rounds
            # as it was, rather than failing the resume.
            with contextlib.suppress(TypeError, ValueError):
                new_state["max_rounds"] = int(str(new_state["round"])) + rounds
        row.state = new_state
        row.current_phase = target_phase
        row.status = "active"  # a continued flow is live again, not 'completed'
        # Reset the scheduler cursor. It only auto-resets on a phase_key *change*
        # (core.process.scheduler: ``raw_cursor.phase_key != ctx.phase_key``); re-opening a
        # flow at a phase it has already run (e.g. the round-table's ``discussion``) keeps the
        # same phase_key, so without this the scheduler would resume the exhausted rotation and
        # transition straight back out with no fresh turns. An empty cursor reads as "fresh"
        # (its absent phase_key never matches), starting the reopened phase's rotation over.
        row.actor_cursor = {}
        await session.flush()
        return row


async def archive_session(tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
    """Soft-delete: stamp ``archived_at`` so the session drops out of ``list_sessions``. Not
    a pause and not a delete -- its append-only messages/manifests/resolutions stay intact."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            raise ValueError(f"no session {session_id} in this tenant")
        if row.archived_at is None:
            row.archived_at = datetime.now(UTC)
        await session.flush()


async def pause_session(tenant_id: uuid.UUID, session_id: uuid.UUID) -> SessionRow:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            raise ValueError(f"no session {session_id} in this tenant")
        row.status = "paused"
        await session.flush()
        return row


async def resume_session(tenant_id: uuid.UUID, session_id: uuid.UUID) -> SessionRow:
    """also clears any *failed* idempotency record for this session's current
    turn (``turn:{session_id}:{next_event_seq}``) before flipping status back to
    active -- a session paused by ``core.process.interpreter.advance_session`` after a
    transient provider failure (all retries + fallback exhausted inside a turn) would
    otherwise immediately re-raise ``OperationFailedError`` on the very next advance,
    since ``@idempotent`` never un-poisons a failed key on its own
    (``core.actions.idempotency.clear_failed_operation``'s own docstring). A no-op,
    harmless call when the session was paused for any other reason (nothing to clear)."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            raise ValueError(f"no session {session_id} in this tenant")
        row.status = "active"
        next_event_seq = row.next_event_seq
        await session.flush()

    await clear_failed_operation(tenant_id, f"turn:{session_id}:{next_event_seq}")

    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        return row
