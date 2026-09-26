"""EntitySchema repository. Not INV-1-restricted -- entity schemas/data are not
knowledge or secrets; nothing about them is "stored text that reaches a model" in the
sense INV-1 protects (they're deterministically injected, not retrieved). Freely
importable, unlike ``core.secrets.repo``/``core.knowledge.repo``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.entities.schema import EntitySchemaDefinition, EntitySchemaRow
from core.entities.validation import SchemaValidationError, validate_schema_definition
from core.tenancy.scope import tenant_scope


async def save_schema(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    key: str,
    version: int,
    definition: EntitySchemaDefinition,
    *,
    created_by: uuid.UUID | None = None,
    ai_assisted: bool = False,
) -> EntitySchemaRow:
    """Validates (CEL compile-check on derived/constraints) then inserts a new immutable
    version row. Raises ``SchemaValidationError`` (carrying every field-level issue) if
    validation fails -- nothing is written in that case."""
    issues = validate_schema_definition(definition)
    if issues:
        raise SchemaValidationError(issues)

    async with tenant_scope(tenant_id) as session:
        row = EntitySchemaRow(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            key=key,
            version=version,
            fields=[f.model_dump(mode="json") for f in definition.fields],
            derived=[d.model_dump(mode="json") for d in definition.derived],
            constraints=[c.model_dump(mode="json") for c in definition.constraints],
            state_machines=[
                m.model_dump(mode="json", by_alias=True) for m in definition.state_machines
            ],
            views=[v.model_dump(mode="json") for v in definition.views],
            created_by=created_by,
            ai_assisted=ai_assisted,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


async def get_schema(tenant_id: uuid.UUID, schema_id: uuid.UUID) -> EntitySchemaRow | None:
    async with tenant_scope(tenant_id) as session:
        return await session.get(EntitySchemaRow, schema_id)


async def get_latest_schema_version(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID | None, key: str
) -> EntitySchemaRow | None:
    async with tenant_scope(tenant_id) as session:
        result: EntitySchemaRow | None = await session.scalar(
            select(EntitySchemaRow)
            .where(
                EntitySchemaRow.tenant_id == tenant_id,
                EntitySchemaRow.workspace_id == workspace_id,
                EntitySchemaRow.key == key,
            )
            .order_by(EntitySchemaRow.version.desc())
            .limit(1)
        )
        return result


async def next_version(tenant_id: uuid.UUID, workspace_id: uuid.UUID | None, key: str) -> int:
    existing = await get_latest_schema_version(tenant_id, workspace_id, key)
    return 1 if existing is None else existing.version + 1


async def list_schema_versions(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID | None, key: str
) -> list[EntitySchemaRow]:
    """Every version of one schema key, newest first -- the version history panel
    (`list_latest_schemas` deliberately collapses to one row per key, so it can't serve
    this; a distinct query, not a filter over that one's results)."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(EntitySchemaRow)
                .where(
                    EntitySchemaRow.tenant_id == tenant_id,
                    EntitySchemaRow.workspace_id == workspace_id,
                    EntitySchemaRow.key == key,
                )
                .order_by(EntitySchemaRow.version.desc())
            )
        ).scalars()
        return list(rows)


async def list_latest_schemas(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID | None
) -> list[EntitySchemaRow]:
    """The latest version of each distinct schema key for this ``(tenant, workspace)``
    pair -- ``workspace_id=None`` is the template gallery (pack-provided schemas,
    never tied to one workspace). Small counts (a handful of schemas per pack/
    workspace), so "fetch all, keep the max version per key in Python" is simpler than a
    window-function query and not a real cost at this scale."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(EntitySchemaRow).where(
                        EntitySchemaRow.tenant_id == tenant_id,
                        EntitySchemaRow.workspace_id == workspace_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    latest_by_key: dict[str, EntitySchemaRow] = {}
    for row in rows:
        current = latest_by_key.get(row.key)
        if current is None or row.version > current.version:
            latest_by_key[row.key] = row
    return sorted(latest_by_key.values(), key=lambda r: r.key)
