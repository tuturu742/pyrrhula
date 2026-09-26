"""Checkpoints: derived snapshots written at every phase
transition, enabling resume, fork, and time-travel. The session log
(``session_event``) is the source of truth and is append-only; a checkpoint is a
convenience snapshot *of* it, not a second source of truth -- ``reconstruct_state``
below proves this by rebuilding the same state purely from a checkpoint plus the
session_event tail after it, independent of whatever the live ``session`` row currently
holds.

``make_checkpoint_hook`` produces the exact ``CheckpointHook`` seam
(``Callable[[InterpreterContext], Awaitable[None]]``, left as an optional no-op there) --
this is the real implementation the docstring said would land here.

``knowledge_version_pins`` is real (the ``resolve_effective_version_id`` per attached
source); ``entity_versions`` is real too now -- every entity in the workspace
pinned at its current version, the same "everything, not a viewer's subset" shape
``knowledge_version_pins`` already uses (a checkpoint is for fork/resume correctness,
not visibility).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from core.entities.storage import list_all_entities_for_workspace
from core.knowledge.authoring import list_workspace_attachments
from core.knowledge.versioning import resolve_effective_version_id
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.interpreter import CheckpointHook, InterpreterContext
from core.process.models import ProcessDefinitionRow
from core.sessions.models import CheckpointRow, SessionEventRow, SessionRow
from core.tenancy.scope import tenant_scope


async def _compute_knowledge_version_pins(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> dict[str, str | None]:
    attachments = await list_workspace_attachments(tenant_id, workspace_id)
    pins: dict[str, str | None] = {}
    for attachment in attachments:
        version_id = await resolve_effective_version_id(
            tenant_id, workspace_id, attachment.knowledge_source_id
        )
        pins[str(attachment.knowledge_source_id)] = str(version_id) if version_id else None
    return pins


async def _compute_entity_version_pins(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> dict[str, int]:
    entities = await list_all_entities_for_workspace(tenant_id, workspace_id)
    return {str(entity.id): entity.version for entity in entities}


async def write_checkpoint(
    tenant_id: uuid.UUID, session_id: uuid.UUID, workspace_id: uuid.UUID
) -> CheckpointRow:
    """Snapshots the session's *current* phase/state/actor_cursor. Called at every phase
    transition (via ``make_checkpoint_hook``) -- append-only, one row per transition, the
    same shape ``knowledge_source_version``/``audit_log`` use for "historical fact, never
    edited after the fact"."""
    knowledge_version_pins = await _compute_knowledge_version_pins(tenant_id, workspace_id)
    entity_versions = await _compute_entity_version_pins(tenant_id, workspace_id)

    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        checkpoint = CheckpointRow(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=row.next_event_seq,
            phase=row.current_phase,
            state=dict(row.state),
            actor_cursor=dict(row.actor_cursor),
            entity_versions=entity_versions,
            knowledge_version_pins=knowledge_version_pins,
        )
        session.add(checkpoint)
        await session.flush()
        return checkpoint


def make_checkpoint_hook(workspace_id: uuid.UUID) -> CheckpointHook:
    """the ``CheckpointHook`` seam, for real: pass this to
    ``advance_session(checkpoint_hook=...)`` to write a real checkpoint at every
    transition instead of the documented no-op default."""

    async def hook(ctx: InterpreterContext) -> None:
        await write_checkpoint(ctx.tenant_id, ctx.session_id, workspace_id)

    return hook


async def list_checkpoints(tenant_id: uuid.UUID, session_id: uuid.UUID) -> list[CheckpointRow]:
    """the fork-from-checkpoint UI's picker list -- oldest first, same ordering
    ``reconstruct_state`` walks forward from."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(CheckpointRow)
                .where(CheckpointRow.session_id == session_id)
                .order_by(CheckpointRow.event_seq)
            )
        ).scalars()
        return list(rows)


async def _latest_checkpoint(tenant_id: uuid.UUID, session_id: uuid.UUID) -> CheckpointRow | None:
    async with tenant_scope(tenant_id) as session:
        result: CheckpointRow | None = await session.scalar(
            select(CheckpointRow)
            .where(CheckpointRow.session_id == session_id)
            .order_by(CheckpointRow.event_seq.desc())
            .limit(1)
        )
        return result


async def reconstruct_state(
    tenant_id: uuid.UUID, session_id: uuid.UUID
) -> tuple[str, dict[str, object]]:
    """Rebuilds ``(current_phase, state)`` purely from the latest checkpoint plus the
    session_event tail after it -- proves the log is genuinely the source of truth, not
    merely a description of one. Requires at least one checkpoint to exist (a session
    that hasn't transitioned even once has nothing to "resume" mid-way through; that's
    just ``start_session``'s own recorded starting point, not a resume scenario).

    Only ``phase_transition`` events change ``(phase, state)`` -- ``message`` events
    don't mutate state (see ``core.process.interpreter``), so they're skipped here.
    ``actor_cursor`` is deliberately *not* reconstructed by this function: it is
    continuously, transactionally live-persisted on the session row by the scheduler
     independent of checkpoint boundaries, and no event kind records its
    turn-by-turn deltas -- reconstructing it from the log alone isn't possible with the
    current event schema, a documented boundary, not a silent gap.
    """
    checkpoint = await _latest_checkpoint(tenant_id, session_id)
    if checkpoint is None:
        raise ValueError(f"session {session_id} has no checkpoint to reconstruct from")

    phase = checkpoint.phase
    state = dict(checkpoint.state)

    async with tenant_scope(tenant_id) as session:
        trailing = (
            await session.execute(
                select(SessionEventRow)
                .where(
                    SessionEventRow.session_id == session_id,
                    SessionEventRow.event_seq >= checkpoint.event_seq,
                    SessionEventRow.kind == "phase_transition",
                )
                .order_by(SessionEventRow.event_seq)
            )
        ).scalars()
        for event in trailing:
            to_phase = event.payload["to"]
            from_phase = event.payload["from"]
            assert isinstance(from_phase, str)
            phase = to_phase if isinstance(to_phase, str) else from_phase  # None = terminal
            payload_state = event.payload["state"]
            assert isinstance(payload_state, dict)
            state = dict(payload_state)

    return phase, state


class PinnedDefinitionMissingError(Exception):
    """The session's pinned ``process_definition_id`` no longer resolves. Resume refuses
    rather than falling back to "the newest version of that key" -- silently running a
    dormant session against an edited definition is precisely the failure this resume path
    exists to make impossible."""


@dataclass(frozen=True)
class RestoredSession:
    """What a long-dormant session needs to run its next turn. ``phase``/``state``
    come from ``reconstruct_state`` (checkpoint + event tail -- the log is the source of
    truth), ``actor_cursor`` and the two pin maps from the checkpoint itself, and
    ``definition`` from the session's *pinned* ``process_definition_id``."""

    session_id: uuid.UUID
    phase: str
    state: dict[str, object]
    actor_cursor: dict[str, object]
    entity_versions: dict[str, object]
    knowledge_version_pins: dict[str, object]
    process_definition_id: uuid.UUID | None
    process_definition_version: int | None
    definition: ProcessDefinitionDSL | None
    resumed_from_event_seq: int


async def restore_session_from_checkpoint(
    tenant_id: uuid.UUID, session_id: uuid.UUID
) -> RestoredSession:
    """The hardened resume path for a session that has been dormant long enough for the
    world around it to have moved.

    Three things it does that a naive "read the session row" resume does not:

    1. **Rebuilds ``(phase, state)`` from the log**, via ``reconstruct_state`` -- and
       writes the result back onto the session row. Deterministic and idempotent: when the
       row already agrees (the normal case) this is a no-op write, and when it doesn't,
       the log wins, because the log is the source of truth and the row is a cache of it.
    2. **Resolves the process definition by its pinned id**, never by "latest version of
       this key". ``process_definition`` rows are immutable one-version-each, so
       pinning by id *is* pinning by version -- an edit published while the session slept
       created a different row and does not apply here. Upgrading is
       ``upgrade_session_definition``, an explicit action a human takes, and the version
       drift is visible in ``RestoredSession.process_definition_version`` before they do.
    3. **Carries the checkpoint's ``entity_versions`` and ``knowledge_version_pins``
       forward** as read pins for the resumed turn's context -- it does *not* write them
       back onto the entities. Rolling live entity state backwards to match a month-old
       checkpoint would be data loss dressed as a restore; that operation exists already
       and is called ``fork_session``.

    A session with no checkpoint at all has nothing to resume mid-way through and raises
    from ``reconstruct_state`` -- see that function's own docstring.
    """
    checkpoint = await _latest_checkpoint(tenant_id, session_id)
    if checkpoint is None:
        raise ValueError(f"session {session_id} has no checkpoint to resume from")

    phase, state = await reconstruct_state(tenant_id, session_id)

    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            raise ValueError(f"no session {session_id} in this tenant")
        row.current_phase = phase
        row.state = dict(state)
        row.actor_cursor = dict(checkpoint.actor_cursor)
        definition_id = row.process_definition_id
        definition_version = row.process_definition_version

        definition: ProcessDefinitionDSL | None = None
        if definition_id is not None:
            definition_row = await session.get(ProcessDefinitionRow, definition_id)
            if definition_row is None:
                raise PinnedDefinitionMissingError(
                    f"session {session_id} pins process_definition {definition_id}, which no "
                    "longer exists in this tenant"
                )
            definition = ProcessDefinitionDSL.model_validate(definition_row.definition)
            definition_version = definition_row.version

    return RestoredSession(
        session_id=session_id,
        phase=phase,
        state=dict(state),
        actor_cursor=dict(checkpoint.actor_cursor),
        entity_versions=dict(checkpoint.entity_versions),
        knowledge_version_pins=dict(checkpoint.knowledge_version_pins),
        process_definition_id=definition_id,
        process_definition_version=definition_version,
        definition=definition,
        resumed_from_event_seq=checkpoint.event_seq,
    )


async def upgrade_session_definition(
    tenant_id: uuid.UUID, session_id: uuid.UUID, target_definition_id: uuid.UUID
) -> RestoredSession:
    """The explicit half of "an edited definition does not silently apply": move a session
    onto a different published version of its process definition, deliberately. Refuses to
    cross to a *different* definition key -- that isn't an upgrade, it's a different
    process, and a session halfway through one graph cannot meaningfully continue in
    another whose phase keys it has never heard of."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            raise ValueError(f"no session {session_id} in this tenant")
        target = await session.get(ProcessDefinitionRow, target_definition_id)
        if target is None:
            raise PinnedDefinitionMissingError(
                f"no process_definition {target_definition_id} in this tenant"
            )
        if row.process_definition_id is not None:
            current = await session.get(ProcessDefinitionRow, row.process_definition_id)
            if current is not None and current.key != target.key:
                raise ValueError(
                    f"session {session_id} runs definition key {current.key!r}; refusing to "
                    f"upgrade it onto a different key {target.key!r}"
                )
        row.process_definition_id = target.id
        row.process_definition_version = target.version

    return await restore_session_from_checkpoint(tenant_id, session_id)


async def fork_session(
    tenant_id: uuid.UUID,
    checkpoint_id: uuid.UUID,
    *,
    created_by: uuid.UUID | None = None,
) -> SessionRow:
    """A new session rooted at ``checkpoint_id``: same workspace/agent/definition, state
    exactly as the checkpoint recorded it, ``forked_from_checkpoint_id`` set for
    provenance. The parent session is never touched -- no shared mutable state, proven by
    the isolation test (the two sessions' ``state``/``current_phase`` diverge freely from
    the moment of fork onward, each in its own row).
    """
    async with tenant_scope(tenant_id) as session:
        checkpoint = await session.get(CheckpointRow, checkpoint_id)
        if checkpoint is None:
            raise ValueError(f"no checkpoint {checkpoint_id} in this tenant")
        parent = await session.get(SessionRow, checkpoint.session_id)
        assert parent is not None

        forked = SessionRow(
            tenant_id=tenant_id,
            workspace_id=parent.workspace_id,
            persona_id=parent.persona_id,
            current_phase=checkpoint.phase,
            state=dict(checkpoint.state),
            actor_cursor=dict(checkpoint.actor_cursor),
            process_definition_id=parent.process_definition_id,
            process_definition_version=parent.process_definition_version,
            forked_from_checkpoint_id=checkpoint_id,
        )
        session.add(forked)
        await session.flush()
        return forked
