"""ResolutionService (C1.6, plan §9.1/§9.2 (D5)/§12.7, INV-7): the trust chain for
mechanical results end to end -- request, validate (C1.5), seeded execute, immutable
record, system-authored fact. The model never reports a result; the database does.

``resolve()`` is the whole chain as one function, deliberately NOT a class -- matches this
project's established module-function style (``core.process.awaits``,
``core.assembler.visibility``, ...) over a service object with no state to hold.

**Idempotency is layered, not singular.** B1.7's ``_dispatch_tool_idempotent`` already
guarantees a retried tool call with the same idempotency key never re-runs the wrapped
handler at all -- so when ``resolve()`` is reached through the real tool loop, a retry
never even calls it a second time. ``resolve()`` carries its *own*, independent guarantee
too (an advisory lock + an existing-row check keyed on ``(session_id, event_seq)``,
returning the already-written record instead of writing a second one) -- defense in
depth, and the only guarantee that exists at all for a caller that reaches ``resolve()``
some other way (the future MCP façade, G4.13, won't go through B1.7's tool loop).
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select, text

from core.agents.tools import ToolContext, ToolHandler, ToolResult
from core.audit.hashing import compute_row_hash
from core.resolution.records import ResolutionRecordRow, compute_seed, resolution_payload, roll_dice
from core.resolution.rule_system import RuleSystemDefinition, resolve_outcome
from core.resolution.validate import ValidationError, validate
from core.sessions.models import SessionRow
from core.tenancy.scope import tenant_scope


class InvalidResolutionError(Exception):
    """The request never became a roll -- illegal expression, wrong modifier, check not
    legal in this phase, ... . Carries the same structured ``ValidationError`` C1.5
    produces, so a caller (the tool handler, an API route) can report it without
    re-deriving what went wrong."""

    def __init__(self, error: ValidationError) -> None:
        self.error = error
        super().__init__(error.message)


async def resolve(
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    event_seq: int,
    tool_key: str,
    actor_entity_id: uuid.UUID | None,
    expression: str,
    check_type: str,
    actor_fields: dict[str, object],
    target: int | None,
    rule_system: RuleSystemDefinition,
    rule_system_id: uuid.UUID,
    legal_check_types: frozenset[str] | None,
    rule_citation_ids: list[uuid.UUID] | None = None,
) -> ResolutionRecordRow:
    validation = validate(expression, check_type, actor_fields, rule_system, legal_check_types)
    if not validation.ok:
        assert validation.error is not None
        raise InvalidResolutionError(validation.error)
    assert validation.parsed is not None
    assert validation.computed_modifier is not None

    async with tenant_scope(tenant_id) as session:
        # Serialises the whole read-existing/read-secret/compute/insert sequence per
        # session, the same advisory-lock shape AuditService uses per tenant -- so two
        # concurrent callers for the same (session_id, event_seq) can't both pass the
        # existing-row check and both insert.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": str(session_id)}
        )

        existing = await session.scalar(
            select(ResolutionRecordRow).where(
                ResolutionRecordRow.session_id == session_id,
                ResolutionRecordRow.event_seq == event_seq,
            )
        )
        if existing is not None:
            return existing  # idempotent: never re-roll on retry

        session_row = await session.get(SessionRow, session_id)
        assert session_row is not None
        if session_row.roll_secret is None:
            session_row.roll_secret = secrets.token_hex(32)
        secret = session_row.roll_secret

        seed = compute_seed(secret, session_id, event_seq, expression)
        roll_result = roll_dice(validation.parsed, validation.computed_modifier, seed)
        outcome = resolve_outcome(roll_result.total, target, rule_system.outcome_bands)

        prev_hash = await session.scalar(
            select(ResolutionRecordRow.row_hash)
            .where(ResolutionRecordRow.session_id == session_id)
            .order_by(ResolutionRecordRow.event_seq.desc())
            .limit(1)
        )
        payload = resolution_payload(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=event_seq,
            tool_key=tool_key,
            actor_entity_id=actor_entity_id,
            expression=expression,
            seed=seed.hex(),
            rolls=list(roll_result.rolls),
            modifiers={"total": roll_result.modifier},
            total=roll_result.total,
            target=target,
            outcome=outcome,
            rule_system_id=rule_system_id,
            rule_citation_ids=rule_citation_ids or [],
        )
        row_hash = compute_row_hash(prev_hash, payload)

        row = ResolutionRecordRow(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=event_seq,
            tool_key=tool_key,
            actor_entity_id=actor_entity_id,
            expression=expression,
            seed=seed.hex(),
            rolls=list(roll_result.rolls),
            modifiers={"total": roll_result.modifier},
            total=roll_result.total,
            target=target,
            outcome=outcome,
            rule_system_id=rule_system_id,
            rule_citation_ids=rule_citation_ids or [],
            prev_hash=prev_hash,
            row_hash=row_hash,
        )
        session.add(row)
        await session.flush()
        return row


def render_resolution_fact(record: ResolutionRecordRow, *, check_type: str) -> str:
    """§9.2 step 5: the result enters context as a SYSTEM-AUTHORED FACT, not something
    the model is asked to compute or restate. ``authoritative="true"`` plus the explicit
    "don't contradict or restate as a different number" instruction is what step 6 (the
    UI rendering from ``ResolutionRecord`` by id, never parsing prose -- INV-7) depends on
    being unambiguous about. Not yet wired into ``assemble()``'s render pipeline -- no
    caller in Phase 1 assembles a context that also runs a resolution in the same turn
    yet (C1.2-C1.5 all flagged the same "not yet integrated into the runtime" gap); this
    is the complete, tested rendering half, ready for that wiring.
    """
    rolls_str = ", ".join(str(r) for r in record.rolls)
    modifier = record.modifiers.get("total", 0)
    sign = "+" if isinstance(modifier, int) and modifier >= 0 else ""
    target_str = f" vs {record.target}" if record.target is not None else ""
    return (
        f'<resolution id="{record.id}" authoritative="true">\n'
        f"{check_type} check: {record.expression} ({rolls_str}){sign}{modifier} = "
        f"{record.total}{target_str} → {record.outcome.upper()}\n"
        f"Narrate this outcome; you may not contradict or restate it as a different number.\n"
        f"</resolution>"
    )


ActorFieldsResolver = Callable[[uuid.UUID | None], Awaitable[dict[str, object]]]


def make_dice_roller_handler(
    *,
    rule_system: RuleSystemDefinition,
    rule_system_id: uuid.UUID,
    legal_check_types: frozenset[str] | None,
    actor_fields_resolver: ActorFieldsResolver,
) -> ToolHandler:
    """The real ``dice_roller`` tool handler (§9.1's internal-function-calling path),
    wired into a ``core.agents.tools.ToolRegistry`` at the composition root. Trusted
    actor state comes from the injected ``actor_fields_resolver`` -- never from the tool
    call's own arguments, which the model controls and could lie in (that would just move
    the hallucination vector from "claimed modifier" to "claimed stats", defeating the
    entire point). No Entity system exists until F3.6 (Phase 3); the resolver is today's
    injection seam for that, matching every other C1.x "not yet real" source.
    """

    async def handler(args: dict[str, object], ctx: ToolContext) -> ToolResult:
        if ctx.session_id is None:
            return ToolResult(
                content=json.dumps(
                    {"error": "no_session", "message": "dice_roller requires a session"}
                )
            )

        expression = str(args["expression"])
        check_type = str(args["check_type"])
        actor_entity_id = (
            uuid.UUID(str(args["actor_entity_id"])) if args.get("actor_entity_id") else None
        )
        target = int(str(args["target"])) if args.get("target") is not None else None

        actor_fields = await actor_fields_resolver(actor_entity_id)

        async with tenant_scope(ctx.tenant_id) as session:
            session_row = await session.get(SessionRow, ctx.session_id)
            assert session_row is not None
            event_seq = session_row.next_event_seq
            session_row.next_event_seq = event_seq + 1

        try:
            record = await resolve(
                tenant_id=ctx.tenant_id,
                session_id=ctx.session_id,
                event_seq=event_seq,
                tool_key="dice_roller",
                actor_entity_id=actor_entity_id,
                expression=expression,
                check_type=check_type,
                actor_fields=actor_fields,
                target=target,
                rule_system=rule_system,
                rule_system_id=rule_system_id,
                legal_check_types=legal_check_types,
            )
        except InvalidResolutionError as exc:
            return ToolResult(
                content=json.dumps({"error": exc.error.code, "message": exc.error.message})
            )

        return ToolResult(
            content=json.dumps(
                {"resolution_id": str(record.id), "total": record.total, "outcome": record.outcome}
            )
        )

    return handler
