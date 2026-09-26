"""``resolution_record`` (INV-7/INV-10): the immutable,
hash-chained trail of every mechanical result. Everything downstream -- the resolution widget,
citations, replay -- reads this table by id, never a model's prose (INV-7).

**Seeded execution.** ``seed = HMAC(session_secret, session_id || event_seq
|| expression)`` -- a session-scoped secret (``session.roll_secret``, generated lazily on
first use) means the seed is unpredictable to a player in advance (they don't know the
secret) but fully *reproducible* after the fact if the secret is disclosed: same secret +
same session_id/event_seq/expression -> the same seed -> ``roll_expression`` is a pure function
of that seed, so the same rolls, deterministically. That reproducibility is the actual
anti-cheat property (a player can be shown the seed and independently recompute the roll),
not cryptographic unpredictability of the RNG algorithm itself -- Python's seeded
``random.Random`` is exactly what "replayable" needs here.

**Hash chain** reuses ``core.audit.hashing``'s ``compute_row_hash``/``canonical_json``
directly (the same primitive ``audit_log`` uses) rather than a second implementation that
could quietly drift from it -- but chained *per session* (``session_id``, ordered by
``event_seq``), not per tenant like ``audit_log``, since a resolution record's natural
predecessor is "the last roll in this session," not "the last audit event anywhere in this
tenant."
"""

from __future__ import annotations

import hmac
import random
import uuid
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from sqlalchemy import ARRAY, DateTime, ForeignKey, Index, Integer, String, func, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

# ResolutionRecordRow FKs to session.id and rule_system.id by string reference --
# SQLAlchemy only resolves those at mapper-configuration time, which requires both
# referenced tables' ORM modules to have been imported by *someone* first (the same
# registration-order fix other modules needed). Importing them here guarantees that
# regardless of what a caller of this module imports.
import core.resolution.rule_system  # noqa: E402, F401
import core.sessions.models  # noqa: E402, F401
from core.audit.hashing import compute_row_hash
from core.resolution.grammar import ParsedExpression
from core.tenancy.models import Base


class ResolutionRecordRow(Base):
    __tablename__ = "resolution_record"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_key: Mapped[str] = mapped_column(String(63), nullable=False)
    actor_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    expression: Mapped[str] = mapped_column(String(255), nullable=False)
    seed: Mapped[str] = mapped_column(String(64), nullable=False)  # hex-encoded HMAC digest
    rolls: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    modifiers: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_system_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rule_system.id"), nullable=False
    )
    rule_citation_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list
    )
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("uq_resolution_record_session_seq", "session_id", "event_seq", unique=True),
    )


def compute_seed(
    session_secret: str, session_id: uuid.UUID, event_seq: int, expression: str
) -> bytes:
    message = f"{session_id}|{event_seq}|{expression}".encode()
    return hmac.new(session_secret.encode(), message, sha256).digest()


@dataclass(frozen=True)
class RollResult:
    rolls: tuple[int, ...]
    kept: tuple[int, ...]
    modifier: int
    total: int


def roll_expression(parsed: ParsedExpression, modifier: int, seed: bytes) -> RollResult:
    """Pure function of ``(parsed, modifier, seed)`` -- the whole reproducibility property
    (INV-10) rests on this never consulting anything else (wall-clock,
    unseeded global RNG state, dict ordering)."""
    rng = random.Random(seed)  # noqa: S311 -- deterministic-by-design seeded PRNG, not a security RNG
    rolls = tuple(rng.randint(1, parsed.sides) for _ in range(parsed.count))
    kept = tuple(sorted(rolls, reverse=True)[: parsed.keep]) if parsed.keep is not None else rolls
    return RollResult(rolls=rolls, kept=kept, modifier=modifier, total=sum(kept) + modifier)


def resolution_payload(
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    event_seq: int,
    tool_key: str,
    actor_entity_id: uuid.UUID | None,
    expression: str,
    seed: str,
    rolls: list[int],
    modifiers: dict[str, object],
    total: int,
    target: int | None,
    outcome: str,
    rule_system_id: uuid.UUID,
    rule_citation_ids: list[uuid.UUID],
) -> dict[str, object]:
    return {
        "tenant_id": str(tenant_id),
        "session_id": str(session_id),
        "event_seq": event_seq,
        "tool_key": tool_key,
        "actor_entity_id": str(actor_entity_id) if actor_entity_id else None,
        "expression": expression,
        "seed": seed,
        "rolls": rolls,
        "modifiers": modifiers,
        "total": total,
        "target": target,
        "outcome": outcome,
        "rule_system_id": str(rule_system_id),
        "rule_citation_ids": sorted(str(c) for c in rule_citation_ids),
    }


def _row_payload(row: ResolutionRecordRow) -> dict[str, object]:
    return resolution_payload(
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        event_seq=row.event_seq,
        tool_key=row.tool_key,
        actor_entity_id=row.actor_entity_id,
        expression=row.expression,
        seed=row.seed,
        rolls=list(row.rolls),
        modifiers=row.modifiers,
        total=row.total,
        target=row.target,
        outcome=row.outcome,
        rule_system_id=row.rule_system_id,
        rule_citation_ids=list(row.rule_citation_ids),
    )


async def verify_resolution_chain(session: AsyncSession, session_id: uuid.UUID) -> list[uuid.UUID]:
    """Walks one session's resolution records in ``event_seq`` order, same two checks as
    ``core.audit.verify.verify_chain``: each row's own hash matches its recorded content
    and predecessor, and its ``prev_hash`` matches the actual previous row's ``row_hash``.
    Takes an already-``tenant_scope``'d session (unlike ``verify_chain``, which opens its
    own) so a caller verifying inside a larger transaction -- e.g. a test asserting a
    tamper right after making it -- doesn't need a second, separate connection."""
    rows = (
        (
            await session.execute(
                select(ResolutionRecordRow)
                .where(ResolutionRecordRow.session_id == session_id)
                .order_by(ResolutionRecordRow.event_seq)
            )
        )
        .scalars()
        .all()
    )

    broken: list[uuid.UUID] = []
    expected_prev: str | None = None
    for row in rows:
        expected_hash = compute_row_hash(row.prev_hash, _row_payload(row))
        if expected_hash != row.row_hash or row.prev_hash != expected_prev:
            broken.append(row.id)
        expected_prev = row.row_hash
    return broken
