"""RuleSystem: pack content, tenant-scoped storage. Defines which
mechanical expressions are legal and how modifiers derive from actor state, so the engine
-- not the model -- computes the "+5" ('s anti-hallucination property).

No Entity system exists anywhere in Phase 1 (F3.6 is Phase 3) -- ``modifier_resolver``
(CEL) evaluates over a caller-supplied ``actor_fields: dict[str, object]``, an injection
seam matching the entity-state stub, not a live Entity table read. F3.6 replaces the
*source* of ``actor_fields``, not this module's shape.

Unlike ``process_definition``, this is **not** append-only/versioned: a rule
system is mutable, upserted-by-key content (matching ``knowledge_source``'s shape), not
an immutable history a running session pins to a specific version of. Nothing in the
task list asks for that; if a real need for pinned rule-system versions surfaces later
(mirroring the knowledge versioning), it's an additive change, not a redesign.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

import celpy
from celpy.celparser import CELParseError
from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base
from core.tenancy.scope import tenant_scope

_cel_env = celpy.Environment()


class RuleSystemRow(Base):
    __tablename__ = "rule_system"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    expression_grammar: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    check_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    outcome_bands: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    modifier_resolver: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    validators: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_rule_system_tenant_key"),)


class RuleSystemDefinitionSchema(BaseModel):
    """Validated authoring-time shape, checked by ``validate_definition`` before a
    ``RuleSystemRow`` is written -- mirrors the ``ProcessDefinitionDSL``/validator
    split."""

    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    expression_grammar: dict[str, object]
    check_types: list[str]
    outcome_bands: list[dict[str, object]] = []
    modifier_resolver: dict[str, str]
    validators: list[str] = []


@dataclass(frozen=True)
class RuleSystemDefinition:
    """The plain runtime shape ``core.resolution.validate`` actually consumes --
    decoupled from both the ORM row and the authoring-time Pydantic schema, matching this
    project's established storage/spec separation."""

    key: str
    expression_grammar: dict[str, object]
    check_types: frozenset[str]
    outcome_bands: tuple[dict[str, object], ...] = field(default_factory=tuple)
    modifier_resolver: dict[str, str] = field(default_factory=dict)
    validators: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_row(cls, row: RuleSystemRow) -> RuleSystemDefinition:
        return cls(
            key=row.key,
            expression_grammar=row.expression_grammar,
            check_types=frozenset(row.check_types),
            outcome_bands=tuple(row.outcome_bands),
            modifier_resolver=row.modifier_resolver,
            validators=tuple(row.validators),
        )

    @classmethod
    def from_schema(cls, schema: RuleSystemDefinitionSchema) -> RuleSystemDefinition:
        return cls(
            key=schema.key,
            expression_grammar=schema.expression_grammar,
            check_types=frozenset(schema.check_types),
            outcome_bands=tuple(schema.outcome_bands),
            modifier_resolver=schema.modifier_resolver,
            validators=tuple(schema.validators),
        )


class RuleSystemValidationError(Exception):
    pass


# The outcome of a roll nobody is judging: the total is the result.
UNJUDGED = "recorded"


class OutcomeBandingError(Exception):
    """No outcome band matched a total, and no ``target`` was supplied either -- an
    authoring gap in the rule system (bands should be exhaustive), not a caller error."""


def _compile_check(source: str) -> None:
    """Authoring-time check: the expression must at least parse. Cannot check
    evaluate-safety against ``actor_fields`` the way the ``compile_check`` does for
    process state -- ``actor_fields`` has no fixed declared schema (a d20 system and a
    a banded system use completely different field names) -- a documented, real limitation,
    not a silent gap."""
    try:
        _cel_env.compile(source)
    except CELParseError as exc:
        raise RuleSystemValidationError(f"CEL syntax error in {source!r}: {exc}") from exc


def validate_definition(definition: RuleSystemDefinitionSchema) -> None:
    for expr in definition.modifier_resolver.values():
        _compile_check(expr)
    for predicate in definition.validators:
        _compile_check(predicate)

    unknown_resolvers = set(definition.modifier_resolver) - set(definition.check_types)
    if unknown_resolvers:
        raise RuleSystemValidationError(
            f"modifier_resolver defines check types not in check_types: {sorted(unknown_resolvers)}"
        )


def resolve_outcome(
    total: int, target: int | None, outcome_bands: tuple[dict[str, object], ...]
) -> str:
    """: outcome modes. ``target`` set -> simple threshold (">= target: success", the
    d20-vs-DC shape). ``target`` unset with ``outcome_bands`` declared -> ordered bands
    matched against ``total`` alone (the "10+ / 7-9 / 6 or under" shape), first match wins.

    ``target`` unset and no bands declared -> the roll is not being judged, and the total
    is the whole result. Rolling ability scores, damage, a reaction, a wandering monster:
    a number that nothing succeeds or fails against. A target-based system declares no
    bands by design (Basic Fantasy rolls d20 against a number), so demanding one here
    made every unjudged roll raise -- a character creation phase died on 3d6 totalling 8,
    and the only way out would have been for each such pack to invent a catch-all band
    that then mislabels the real checks.
    """
    if target is not None:
        return "success" if total >= target else "failure"
    for band in outcome_bands:
        min_v, max_v = band.get("min"), band.get("max")
        min_ok = min_v is None or (isinstance(min_v, int) and total >= min_v)
        max_ok = max_v is None or (isinstance(max_v, int) and total <= max_v)
        if min_ok and max_ok:
            return str(band["outcome"])
    if not outcome_bands:
        return UNJUDGED
    # Bands exist and none matched: that IS a gap in the system's own definition, and a
    # silent fallback would hide a band table that does not cover its own randomizer.
    raise OutcomeBandingError(f"no outcome band matches total {total} and no target was given")


async def create_rule_system(
    tenant_id: uuid.UUID, definition: RuleSystemDefinitionSchema
) -> RuleSystemRow:
    """Idempotent upsert by ``(tenant_id, key)`` -- matches ``knowledge_source``'s shape,
    not ``process_definition``'s immutable version history (see module docstring)."""
    validate_definition(definition)
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(RuleSystemRow).where(
                RuleSystemRow.tenant_id == tenant_id, RuleSystemRow.key == definition.key
            )
        )
        if existing is not None:
            existing.name = definition.name
            existing.expression_grammar = definition.expression_grammar
            existing.check_types = definition.check_types
            existing.outcome_bands = definition.outcome_bands
            existing.modifier_resolver = definition.modifier_resolver
            existing.validators = definition.validators
            await session.flush()
            return existing

        row = RuleSystemRow(
            tenant_id=tenant_id,
            key=definition.key,
            name=definition.name,
            expression_grammar=definition.expression_grammar,
            check_types=definition.check_types,
            outcome_bands=definition.outcome_bands,
            modifier_resolver=definition.modifier_resolver,
            validators=definition.validators,
        )
        session.add(row)
        await session.flush()
        return row


async def get_or_create_default_rule_system(tenant_id: uuid.UUID) -> RuleSystemRow:
    """a live turn's ``randomizer`` tool needs *some* ``RuleSystemDefinition`` to
    validate against, and nothing seeds one per-tenant today. One tenant-wide default is
    enough for the exit gate's slice -- no per-ProcessDefinition rule-system link exists
    in the schema, and Phase 1 doesn't need one. ``create_rule_system`` is already an
    idempotent upsert by ``(tenant_id, key)``, so this is just a named call to it with a
    fixed key -- calling it repeatedly (e.g. once per live turn) never creates a second
    row or drifts an existing one, matching ``MINIMAL_D20_SYSTEM``'s own content."""
    return await create_rule_system(tenant_id, MINIMAL_D20_SYSTEM)


async def get_rule_system(tenant_id: uuid.UUID, key: str) -> RuleSystemRow | None:
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(RuleSystemRow).where(
                RuleSystemRow.tenant_id == tenant_id, RuleSystemRow.key == key
            )
        )
        return row


# ── MVP fixtures (its own subtask: a d20-like system + a coin-flip, both exercised by
# the same validator code path with no core branching on system kind -- the real INV-9
# test this task cares about). Richer systems ship as workflow-pack or bundle content,
# F3.7; these are core-neutral-named placeholders for the Phase-1 exit slice only. ──

MINIMAL_D20_SYSTEM = RuleSystemDefinitionSchema(
    key="mvp_d20",
    name="MVP d20 System",
    expression_grammar={
        "allowed_sides": [4, 6, 8, 10, 12, 20],
        "max_term_count": 4,
        "allow_keep_drop": False,
    },
    check_types=["stealth", "strength_check"],
    outcome_bands=[],  # target-based (see resolve_outcome) -- no bands needed
    modifier_resolver={
        "stealth": "(fields.dexterity - 10) / 2",
        "strength_check": "(fields.strength - 10) / 2",
    },
    validators=[],
)

COIN_FLIP_SYSTEM = RuleSystemDefinitionSchema(
    key="coin_flip",
    name="Coin Flip",
    expression_grammar={"allowed_sides": [2], "max_term_count": 1, "allow_keep_drop": False},
    check_types=["call"],
    outcome_bands=[
        {"min": 1, "max": 1, "outcome": "tails"},
        {"min": 2, "max": 2, "outcome": "heads"},
    ],
    modifier_resolver={"call": "0"},
    validators=[],
)
