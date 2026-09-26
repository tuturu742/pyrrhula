"""its own isolation + acceptance tests: the generated-column/index generator, its
reversibility, and the ``scope_key`` INV-4 discipline extended to entity queries.
"""

from __future__ import annotations

import inspect
import uuid

import pytest
from sqlalchemy import select, text

from core.entities.repo import save_schema
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import (
    add_indexed_field_column,
    create_entity,
    drop_indexed_field_column,
    generated_column_name,
    list_entities_for_scope,
)
from core.tenancy.models import Workspace
from core.tenancy.scope import admin_ddl_session, tenant_scope


async def _workspace_id(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def test_indexed_field_produces_generated_column_used_by_planner(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)

    definition = EntitySchemaDefinition(
        fields=[FieldDef(key="initiative", type="integer", indexed=True)]
    )
    schema_row = await save_schema(tenant_a, workspace_id, "planner-fixture", 1, definition)
    column = generated_column_name(schema_row.id, "initiative")

    try:
        await add_indexed_field_column(schema_row.id, definition.fields[0])

        await create_entity(
            tenant_a,
            workspace_id,
            schema_row.id,
            definition,
            key="e1",
            name="Entity One",
            scope_key="workspace_public",
            data={"initiative": 17},
        )

        async with admin_ddl_session() as session:
            # A single-row table is cheap enough that the planner may prefer a seq scan
            # on cost alone even with a usable index present; forcing seqscan off for
            # this one, local, throwaway-transaction EXPLAIN isolates the actual claim
            # (an index the planner *can* use exists) from unrelated cost-model noise.
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            plan = (
                await session.execute(
                    text(
                        f"EXPLAIN SELECT * FROM entity WHERE schema_id = :schema_id "
                        f'AND "{column}" = 17'
                    ),
                    {"schema_id": str(schema_row.id)},
                )
            ).all()
        plan_text = "\n".join(row[0] for row in plan)
        assert f"ix_entity_{column}" in plan_text, plan_text
    finally:
        await drop_indexed_field_column(schema_row.id, "initiative")


async def test_index_generator_migrations_are_reversible(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)

    definition = EntitySchemaDefinition(
        fields=[FieldDef(key="budget_remaining", type="number", indexed=True)]
    )
    schema_row = await save_schema(tenant_a, workspace_id, "reversible-fixture", 1, definition)
    field = definition.fields[0]
    column = generated_column_name(schema_row.id, "budget_remaining")

    async def _column_exists() -> bool:
        async with admin_ddl_session() as session:
            result = await session.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'entity' AND column_name = :column"
                ),
                {"column": column},
            )
            return result.first() is not None

    # upgrade -> downgrade -> upgrade, clean each time.
    await add_indexed_field_column(schema_row.id, field)
    assert await _column_exists()
    await drop_indexed_field_column(schema_row.id, "budget_remaining")
    assert not await _column_exists()
    await add_indexed_field_column(schema_row.id, field)
    assert await _column_exists()
    await drop_indexed_field_column(schema_row.id, "budget_remaining")
    assert not await _column_exists()


def test_entity_queries_require_scope_key_with_no_default() -> None:
    sig = inspect.signature(list_entities_for_scope)
    assert sig.parameters["scope_keys"].default is inspect.Parameter.empty

    with pytest.raises(TypeError):
        list_entities_for_scope(  # type: ignore[call-arg, unused-coroutine]
            tenant_id=uuid.uuid4(), workspace_id=uuid.uuid4()
        )


async def test_entity_cross_tenant_filter_omission_returns_zero_rows(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    definition = EntitySchemaDefinition(fields=[FieldDef(key="name", type="string")])

    for tenant_id in (tenant_a, tenant_b):
        workspace_id = await _workspace_id(tenant_id)
        schema_row = await save_schema(
            tenant_id, workspace_id, "cross-tenant-entity", 1, definition
        )
        await create_entity(
            tenant_id,
            workspace_id,
            schema_row.id,
            definition,
            key="e1",
            name="Entity",
            scope_key="workspace_public",
            data={"name": "x"},
        )

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM entity"))).all()

    assert {row[0] for row in rows} == {tenant_a}
