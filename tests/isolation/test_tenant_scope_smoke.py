"""tenant_scope() smoke test (T0.2). See conftest.py for what T0.4 adds on top: the full
per-table matrix, the pooler-leak test, and the library-tenant matrix.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from core.tenancy.scope import tenant_scope, unscoped_session


async def test_filter_omission_returns_zero_foreign_rows(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A raw query with no WHERE tenant_id clause at all, scoped to tenant A, must still
    only ever see tenant A's rows — RLS is doing the filtering, not the query."""
    tenant_a, tenant_b = two_tenants

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM principal"))).all()

    tenant_ids_seen = {row[0] for row in rows}
    assert tenant_ids_seen == {tenant_a}
    assert tenant_b not in tenant_ids_seen


async def test_cross_tenant_read_returns_zero_rows(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants

    async with tenant_scope(tenant_b) as session:
        rows = (await session.execute(text("SELECT id FROM workspace"))).all()

    async with tenant_scope(tenant_a) as session:
        foreign_rows = (
            await session.execute(
                text("SELECT id FROM workspace WHERE id = ANY(:ids)"),
                {"ids": [r[0] for r in rows]},
            )
        ).all()

    assert foreign_rows == []


async def test_unscoped_session_sees_nothing_on_tenant_scoped_tables(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Using the wrong session opener for a tenant-scoped table must fail safe (zero
    rows), never fail open (all tenants' rows)."""
    async with unscoped_session() as session:
        rows = (await session.execute(text("SELECT tenant_id FROM principal"))).all()

    assert rows == []
