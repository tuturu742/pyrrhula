"""Raised hands for an open floor (``order: hands``).

A speaker who wants the floor again calls ``raise_hand`` during their turn. The raise is
recorded against the turn (``floor_request``), not kept in a model's memory or parsed
out of its words, and the scheduler reads it before choosing the next speaker. A hand
counts as up from the turn that raised it until the asker's next turn.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.agents.tools import ToolContext, ToolHandler, ToolResult
from core.ports.model_provider import ToolSpec
from core.process import participants as parts
from core.process.dsl.schema import ActorSpec, PhaseSpec
from core.sessions.models import FloorRequestRow
from core.tenancy.scope import tenant_scope

RAISE_HAND_TOOL_NAME = "raise_hand"


@dataclass(frozen=True)
class RaisedHand:
    principal_id: uuid.UUID
    turn_seq: int


async def raise_hand(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    phase_key: str,
    principal_id: uuid.UUID,
    turn_seq: int,
) -> bool:
    """Record one raised hand for the turn at ``turn_seq``. False when this turn already
    raised it -- a resumed turn's second call changes nothing."""
    async with tenant_scope(tenant_id) as session:
        result = await session.execute(
            pg_insert(FloorRequestRow)
            .values(
                tenant_id=tenant_id,
                session_id=session_id,
                phase_key=phase_key,
                principal_id=principal_id,
                turn_seq=turn_seq,
            )
            .on_conflict_do_nothing(constraint="uq_floor_request_turn")
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]


async def raised_hands(
    tenant_id: uuid.UUID, session_id: uuid.UUID, phase_key: str, since_seq: int
) -> list[RaisedHand]:
    """Every hand raised in this phase at or after ``since_seq`` (the phase's entry), in
    the order the turns that raised them happened."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(FloorRequestRow.principal_id, FloorRequestRow.turn_seq)
                .where(
                    FloorRequestRow.session_id == session_id,
                    FloorRequestRow.phase_key == phase_key,
                    FloorRequestRow.turn_seq >= since_seq,
                )
                .order_by(FloorRequestRow.turn_seq, FloorRequestRow.created_at)
            )
        ).all()
    return [RaisedHand(principal_id=pid, turn_seq=seq) for pid, seq in rows]


def floor_entry(phase: PhaseSpec, actor_cursor: dict[str, Any], phase_key: str) -> ActorSpec | None:
    """The "hands" actor entry the turn now running was scheduled from, read from the
    session's persisted cursor; None when the turn did not come from an open floor."""
    if actor_cursor.get("phase_key") != phase_key:
        return None
    index = actor_cursor.get("entry_index", 0)
    if not isinstance(index, int) or not 0 <= index < len(phase.actors):
        return None
    spec = phase.actors[index]
    return spec if spec.order == "hands" else None


def turns_left(
    spec: ActorSpec,
    actor_cursor: dict[str, Any],
    principal_id: uuid.UUID,
    settings: dict[str, Any],
) -> int | None:
    """How many more turns this speaker may get after the one now running; None when the
    entry sets no per-actor cap."""
    each = parts.setting_number(spec.max_turns_each, settings)
    if each is None:
        return None
    entry = actor_cursor.get("entry") or {}
    spoken = (entry.get("spoken") or {}).get(str(principal_id), 0)
    return max(0, each - int(spoken))


def make_raise_hand_tool(
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    phase_key: str,
    principal_id: uuid.UUID,
    turns_left: int | None,
) -> tuple[ToolSpec, ToolHandler]:
    """The ``raise_hand`` tool for one speaker's turn. ``turns_left`` (after this one) is
    said in the description; a speaker with none left is not offered the tool at all."""
    if turns_left is None:
        left = ""
    else:
        left = f" You have {turns_left} more turn{'s' if turns_left != 1 else ''} at most."
    spec = ToolSpec(
        name=RAISE_HAND_TOOL_NAME,
        description=(
            "Raise your hand to speak again later in this discussion. Your hand stays up "
            "until your next turn; people are called in the order they raised their hands, "
            "after anyone just named. Raise it only when you have something new to say or "
            "must answer someone; the discussion ends when no hand is up. Say your piece "
            "first: raising your hand ends your turn." + left
        ),
        parameters={"type": "object", "properties": {}},
    )

    async def handler(_arguments: dict[str, object], ctx: ToolContext) -> ToolResult:
        if ctx.turn_event_seq is None:
            return ToolResult(json.dumps({"error": "no turn to raise a hand in"}))
        await raise_hand(tenant_id, session_id, phase_key, principal_id, ctx.turn_event_seq)
        # Ends the turn once it has been spoken: a second generation after the raise was
        # where models repeated their speech or wrote the next speakers' lines.
        return ToolResult(json.dumps({"raised": True}), end_turn=True)

    return spec, handler
