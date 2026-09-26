"""ResolutionService (INV-7): the trust chain for
mechanical results end to end -- request, validate, seeded execute, immutable
record, system-authored fact. The model never reports a result; the database does.

``resolve()`` is the whole chain as one function, deliberately NOT a class -- matches this
project's established module-function style (``core.process.awaits``,
``core.assembler.visibility``, ...) over a service object with no state to hold.

**Idempotency is layered, not singular.** the ``_dispatch_tool_idempotent`` already
guarantees a retried tool call with the same idempotency key never re-runs the wrapped
handler at all -- so when ``resolve()`` is reached through the real tool loop, a retry
never even calls it a second time. ``resolve()`` carries its *own*, independent guarantee
too (an advisory lock + an existing-row check keyed on ``(session_id, event_seq)``,
returning the already-written record instead of writing a second one) -- defense in
depth, and the only guarantee that exists at all for a caller that reaches ``resolve()``
some other way (the MCP façade does not go through the tool loop).
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select, text

from core.agents.tools import ToolContext, ToolHandler, ToolResult
from core.audit.hashing import compute_row_hash
from core.resolution.records import (
    ResolutionRecordRow,
    compute_seed,
    resolution_payload,
    roll_expression,
)
from core.resolution.registry import get_tool_definition
from core.resolution.rule_system import RuleSystemDefinition, get_rule_system, resolve_outcome
from core.resolution.validate import ValidationError, validate
from core.sessions.models import SessionRow
from core.tenancy.scope import tenant_scope


class InvalidResolutionError(Exception):
    """The request never became a roll -- illegal expression, wrong modifier, check not
    legal in this phase, ... . Carries the same structured ``ValidationError`` the
    validator produces, so a caller (the tool handler, an API route) can report it without
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
        roll_result = roll_expression(validation.parsed, validation.computed_modifier, seed)
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
    """: the result enters context as a SYSTEM-AUTHORED FACT, not something
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


class UnknownRuleSystemError(Exception):
    """A call named a ``rule_system`` this tenant has not registered. Deliberately an
    error rather than a silent fall-back to the default: resolving a coin flip's ``1d2``
    in a d20 system produces a wrong-but-plausible record, which is exactly the class of
    result INV-7 exists to prevent."""


async def effective_rule_system(
    tenant_id: uuid.UUID,
    requested_key: str | None,
    default: RuleSystemDefinition,
    default_id: uuid.UUID,
    *,
    tool_key: str = "randomizer",
) -> tuple[RuleSystemDefinition, uuid.UUID]:
    """Resolve which rule system governs one call: the call's own selector, then the
    tool definition's ``validation_ref``, then the injected default.

    ``tool_key`` is which definition to read that binding from. It is a parameter
    because a pack may register the one builtin under a domain-appropriate name --
    ``policy_lookup`` over an approval-band system is the same handler as a coin flip
    over a two-band one -- and each of those carries its own ``validation_ref``."""
    if requested_key is not None and requested_key != default.key:
        row = await get_rule_system(tenant_id, requested_key)
        if row is None:
            raise UnknownRuleSystemError(f"no rule system {requested_key!r} in this workspace")
        return RuleSystemDefinition.from_row(row), row.id

    if requested_key is None:
        tool_def = await get_tool_definition(tenant_id, tool_key)
        bound = str(tool_def.validation_ref) if tool_def and tool_def.validation_ref else None
        if bound is not None and bound != default.key:
            row = await get_rule_system(tenant_id, bound)
            if row is not None:
                return RuleSystemDefinition.from_row(row), row.id

    return default, default_id


def make_randomizer_handler(
    *,
    rule_system: RuleSystemDefinition,
    rule_system_id: uuid.UUID,
    legal_check_types: frozenset[str] | None,
    actor_fields_resolver: ActorFieldsResolver,
) -> ToolHandler:
    """The real ``randomizer`` tool handler ('s internal-function-calling path),
    wired into a ``core.agents.tools.ToolRegistry`` at the composition root. Trusted
    actor state comes from the injected ``actor_fields_resolver`` -- never from the tool
    call's own arguments, which the model controls and could lie in (that would just move
    the hallucination vector from "claimed modifier" to "claimed stats", defeating the
    entire point).

    **One tool, many rule systems.** There is no second randomizer for coin flips and no
    third for ungraded numbers: a coin flip is a rule system whose grammar allows ``1d2``
    and whose two outcome bands read "heads"/"tails", and a raw number is a rule system
    with a permissive grammar and no bands at all. Which system governs a given call is
    decided here, most-specific first:

    1. the call's own ``rule_system`` argument, if it names one this tenant has
       registered -- this is what lets a single turn flip a coin *and* roll a check;
    2. the tool definition's ``validation_ref``, the binding a pack authors when its
       tool should always resolve in one system (the same precedence
       ``resolve_and_apply`` applies);
    3. the injected session default.

    The argument is a *selector*, not an escape hatch: it can only name an already-stored
    ``rule_system`` row, whose grammar then validates the expression and whose CEL
    resolves the modifier from trusted actor fields. A model naming a system that does
    not exist gets an error, never an unvalidated roll.
    """

    async def handler(args: dict[str, object], ctx: ToolContext) -> ToolResult:
        if ctx.session_id is None:
            return ToolResult(
                content=json.dumps(
                    {"error": "no_session", "message": "randomizer requires a session"}
                )
            )

        # A tool argument is model output, so every one of these is a thing a model can
        # get wrong, and getting it wrong must cost the call rather than the session.
        # Unguarded, a mistyped entity id raised out of the handler, failed the advance,
        # and recorded the turn as failed -- after which the idempotency guard correctly
        # refused to retry it. A ninety-minute session died at its climax that way,
        # on one malformed uuid.
        missing = [k for k in ("expression", "check_type") if not str(args.get(k) or "").strip()]
        if missing:
            return ToolResult(
                content=json.dumps(
                    {"error": "missing_args", "message": f"required: {', '.join(missing)}"}
                )
            )
        expression = str(args["expression"])
        check_type = str(args["check_type"])
        try:
            actor_entity_id = (
                uuid.UUID(str(args["actor_entity_id"])) if args.get("actor_entity_id") else None
            )
        except ValueError:
            return ToolResult(
                content=json.dumps(
                    {
                        "error": "invalid_args",
                        "message": (
                            f"actor_entity_id {args.get('actor_entity_id')!r} is not a uuid; "
                            "omit it to roll for nobody in particular"
                        ),
                    }
                )
            )
        try:
            target = int(str(args["target"])) if args.get("target") is not None else None
        except ValueError:
            return ToolResult(
                content=json.dumps(
                    {
                        "error": "invalid_args",
                        "message": f"target {args.get('target')!r} is not a number",
                    }
                )
            )

        requested_key = str(args["rule_system"]) if args.get("rule_system") else None
        try:
            effective, effective_id = await effective_rule_system(
                ctx.tenant_id, requested_key, rule_system, rule_system_id
            )
        except UnknownRuleSystemError as exc:
            return ToolResult(
                content=json.dumps({"error": "unknown_rule_system", "message": str(exc)})
            )
        # The phase's declared allowlist still applies to the session's own system; a
        # deliberately selected one is governed by its own check types instead, which is
        # the whole point of selecting it.
        effective_checks = (
            legal_check_types if effective.key == rule_system.key else effective.check_types
        )

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
                tool_key="randomizer",
                actor_entity_id=actor_entity_id,
                expression=expression,
                check_type=check_type,
                actor_fields=actor_fields,
                target=target,
                rule_system=effective,
                rule_system_id=effective_id,
                legal_check_types=effective_checks,
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
