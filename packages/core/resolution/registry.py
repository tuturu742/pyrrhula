"""``ToolDefinition`` (C1.6, plan §9.1): register once, expose twice -- the same registry
entry backs both internal function-calling (B1.7's tool loop, today) and the MCP façade
(G4.13, later); this table is the *declarative metadata* half of that, not the dispatch
mechanism itself. Actual dispatch stays exactly where B1.7 put it: a
``core.agents.tools.ToolRegistry`` mapping a tool key to a real Python handler, wired at
the composition root. ``impl_ref`` here is descriptive ("which built-in implements this"),
not something this module interprets to look up or execute code -- CLAUDE.md rule 10 (no
user-authored code, ever) means "pack-provided" ``impl_ref`` values can only ever mean "a
pack ships a handler the composition root registers," never a stored code string this
module would ``eval``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base
from core.tenancy.scope import tenant_scope


class ToolDefinitionRow(Base):
    __tablename__ = "tool_definition"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # 'deterministic'|'effectful'
    input_schema: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    output_schema: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    impl_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    validation_ref: Mapped[str | None] = mapped_column(String(63), nullable=True)
    determinism: Mapped[str] = mapped_column(String(16), nullable=False)  # 'pure'|'seeded_random'
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_tool_definition_tenant_key"),)


class ToolDefinitionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    kind: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    impl_ref: str
    validation_ref: str | None = None
    determinism: str


DICE_ROLLER_DEFINITION = ToolDefinitionSchema(
    key="dice_roller",
    kind="deterministic",
    input_schema={
        "type": "object",
        "properties": {
            "expression": {"type": "string"},
            "check_type": {"type": "string"},
            "actor_entity_id": {"type": "string"},
            "target": {"type": "integer"},
            "reason": {"type": "string"},
        },
        "required": ["expression", "check_type"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "resolution_id": {"type": "string"},
            "total": {"type": "integer"},
            "outcome": {"type": "string"},
        },
        "required": ["resolution_id", "total", "outcome"],
    },
    impl_ref="builtin:dice_roller",
    validation_ref=None,  # set per-workspace to whichever rule_system.key governs it
    determinism="seeded_random",
)


async def register_tool_definition(
    tenant_id: uuid.UUID, definition: ToolDefinitionSchema
) -> ToolDefinitionRow:
    """Idempotent upsert by ``(tenant_id, key)``, matching ``rule_system``'s shape."""
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(ToolDefinitionRow).where(
                ToolDefinitionRow.tenant_id == tenant_id, ToolDefinitionRow.key == definition.key
            )
        )
        if existing is not None:
            existing.kind = definition.kind
            existing.input_schema = definition.input_schema
            existing.output_schema = definition.output_schema
            existing.impl_ref = definition.impl_ref
            existing.validation_ref = definition.validation_ref
            existing.determinism = definition.determinism
            await session.flush()
            return existing

        row = ToolDefinitionRow(
            tenant_id=tenant_id,
            key=definition.key,
            kind=definition.kind,
            input_schema=definition.input_schema,
            output_schema=definition.output_schema,
            impl_ref=definition.impl_ref,
            validation_ref=definition.validation_ref,
            determinism=definition.determinism,
        )
        session.add(row)
        await session.flush()
        return row


async def ensure_tool_definition(
    tenant_id: uuid.UUID, definition: ToolDefinitionSchema
) -> ToolDefinitionRow:
    """Insert-if-missing -- never overwrites an existing row. The runtime's baseline
    registration (e.g. the builtin dice roller ensured on every turn) must not clobber
    a richer pack-loaded definition, whose ``validation_ref`` binds the tool to its
    rule system; ``register_tool_definition``'s upsert is for content loads only."""
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(ToolDefinitionRow).where(
                ToolDefinitionRow.tenant_id == tenant_id, ToolDefinitionRow.key == definition.key
            )
        )
        if existing is not None:
            return existing
    return await register_tool_definition(tenant_id, definition)


async def list_tool_definitions(tenant_id: uuid.UUID) -> list[ToolDefinitionRow]:
    """Every registered tool definition for a tenant, by key. G4.13's MCP surface is
    registry-driven -- a pack that registers a new deterministic tool gets it exposed
    without a code change -- and that requires a way to ask what is registered, which
    C1.6 never needed (it only ever resolved one tool by key at a time)."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(ToolDefinitionRow)
                .where(ToolDefinitionRow.tenant_id == tenant_id)
                .order_by(ToolDefinitionRow.key)
            )
        ).scalars()
        result = list(rows)
        for row in result:
            session.expunge(row)
        return result


async def get_tool_definition(tenant_id: uuid.UUID, key: str) -> ToolDefinitionRow | None:
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(ToolDefinitionRow).where(
                ToolDefinitionRow.tenant_id == tenant_id, ToolDefinitionRow.key == key
            )
        )
        return row
