"""Behaviour framework storage: `axis_definition` upsert-by-key (matching
`core.resolution.rule_system.create_rule_system`'s idempotent pack-load shape) and
`behavior_profile`'s append-only version history.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from core.behavior.directives import validate_prompt_directive_bindings
from core.behavior.models import AxisDefinitionRow, BehaviorProfileRow
from core.behavior.validation import AxisDefinitionSchema, validate_axis_definition
from core.tenancy.scope import tenant_scope


async def create_axis_definition(
    tenant_id: uuid.UUID, definition: AxisDefinitionSchema
) -> AxisDefinitionRow:
    """Idempotent upsert by `(tenant_id, pack_id, key)` -- a pack load is re-runnable
    without creating duplicates or drifting an existing row, matching
    `create_rule_system`'s shape exactly."""
    validate_axis_definition(definition)
    bindings = [b.model_dump() for b in definition.bindings]
    validate_prompt_directive_bindings(
        definition.key, definition.range_min, definition.range_max, bindings
    )
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(AxisDefinitionRow).where(
                AxisDefinitionRow.tenant_id == tenant_id,
                AxisDefinitionRow.pack_id == definition.pack_id,
                AxisDefinitionRow.key == definition.key,
            )
        )
        if existing is not None:
            existing.label_key = definition.label_key
            existing.range_min = definition.range_min
            existing.range_max = definition.range_max
            existing.stakes = definition.stakes
            existing.default_value = definition.default
            existing.semantics_md = definition.semantics_md
            existing.bindings = bindings
            await session.flush()
            return existing

        row = AxisDefinitionRow(
            tenant_id=tenant_id,
            pack_id=definition.pack_id,
            key=definition.key,
            label_key=definition.label_key,
            range_min=definition.range_min,
            range_max=definition.range_max,
            stakes=definition.stakes,
            semantics_md=definition.semantics_md,
            default_value=definition.default,
            bindings=bindings,
        )
        session.add(row)
        await session.flush()
        return row


async def get_axis_definition(
    tenant_id: uuid.UUID, pack_id: str, key: str
) -> AxisDefinitionRow | None:
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(AxisDefinitionRow).where(
                AxisDefinitionRow.tenant_id == tenant_id,
                AxisDefinitionRow.pack_id == pack_id,
                AxisDefinitionRow.key == key,
            )
        )
        return row


async def list_axis_definitions(tenant_id: uuid.UUID, pack_id: str) -> list[AxisDefinitionRow]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(AxisDefinitionRow).where(
                    AxisDefinitionRow.tenant_id == tenant_id,
                    AxisDefinitionRow.pack_id == pack_id,
                )
            )
        ).scalars()
        return list(rows)


class UnknownAxisError(ValueError):
    """axis_values names a key the pack's axis_definition set does not declare."""


class AxisValueOutOfRangeError(ValueError):
    """A value falls outside its axis's declared [range_min, range_max]."""


async def create_behavior_profile(
    tenant_id: uuid.UUID,
    persona_id: uuid.UUID,
    pack_id: str,
    axis_values: dict[str, int],
    *,
    created_by: uuid.UUID | None = None,
) -> BehaviorProfileRow:
    """Always a new version -- there is no update path (append-only, CLAUDE.md rule 5).
    `version` is 1 + the agent's current max, computed in the same transaction as the
    insert so two concurrent authors racing to set a new profile still each get a
    distinct version (the `UNIQUE(persona_id, version)` constraint is the actual
    correctness guarantee; this is just making the common case not collide).

    Values are validated against the pack's axis definitions BEFORE the version is
    minted: an unknown key or out-of-range value is the author's bug, and an append-only
    history should not accumulate versions that never meant anything."""
    definitions = {d.key: d for d in await list_axis_definitions(tenant_id, pack_id)}
    for key, value in axis_values.items():
        axis = definitions.get(key)
        if axis is None:
            raise UnknownAxisError(
                f"axis {key!r} is not defined by pack {pack_id!r}; known: {sorted(definitions)}"
            )
        if not axis.range_min <= int(value) <= axis.range_max:
            raise AxisValueOutOfRangeError(
                f"{key}={value} outside [{axis.range_min}, {axis.range_max}]"
            )
    async with tenant_scope(tenant_id) as session:
        current_max = await session.scalar(
            select(func.max(BehaviorProfileRow.version)).where(
                BehaviorProfileRow.persona_id == persona_id
            )
        )
        row = BehaviorProfileRow(
            tenant_id=tenant_id,
            persona_id=persona_id,
            version=(current_max or 0) + 1,
            pack_id=pack_id,
            axis_values=axis_values,
            created_by=created_by,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


async def get_current_behavior_profile(
    tenant_id: uuid.UUID, persona_id: uuid.UUID
) -> BehaviorProfileRow | None:
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(BehaviorProfileRow)
            .where(BehaviorProfileRow.persona_id == persona_id)
            .order_by(BehaviorProfileRow.version.desc())
            .limit(1)
        )
        return row


async def get_behavior_profile_version(
    tenant_id: uuid.UUID, persona_id: uuid.UUID, version: int
) -> BehaviorProfileRow | None:
    """The replay-precision read (INV-10): a manifest pins a specific version, not
    "whatever the agent's profile is now" -- resolving it later must reproduce exactly
    that version even if the agent has since been re-tuned."""
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(BehaviorProfileRow).where(
                BehaviorProfileRow.persona_id == persona_id, BehaviorProfileRow.version == version
            )
        )
        return row


async def list_behavior_profile_versions(
    tenant_id: uuid.UUID, persona_id: uuid.UUID
) -> list[BehaviorProfileRow]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(BehaviorProfileRow)
                .where(BehaviorProfileRow.persona_id == persona_id)
                .order_by(BehaviorProfileRow.version)
            )
        ).scalars()
        return list(rows)
