"""Catalog-based RLS coverage check (T0.4). Doesn't need seeded data or per-table
knowledge: it walks Postgres's own catalogs and asserts every table with a ``tenant_id``
column has RLS enabled *and* forced, except the one documented exception.

This is what catches "added a tenant-scoped table, forgot the RLS policy" automatically,
without relying on anyone remembering to update a test when a new table appears.
"""

from __future__ import annotations

from sqlalchemy import text

from core.tenancy.scope import unscoped_session

# `job` has a tenant_id column but is deliberately not RLS-covered — see
# core.ports.job_queue module docstring. Any other tenant_id-bearing table without RLS
# is a bug, not a documented exception, and this is the single named allowlist entry.
_DOCUMENTED_NO_RLS_EXCEPTION = "job"

_CATALOG_QUERY = """
    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind = 'r'
      AND EXISTS (
          SELECT 1 FROM information_schema.columns col
          WHERE col.table_schema = 'public'
            AND col.table_name = c.relname
            AND col.column_name = 'tenant_id'
      )
"""


async def test_every_tenant_id_table_has_forced_rls(db_available: None) -> None:
    async with unscoped_session() as session:
        rows = (await session.execute(text(_CATALOG_QUERY))).all()

    assert rows, "expected at least one tenant_id-bearing table to exist by T0.4"

    missing_rls = [
        name
        for name, enabled, forced in rows
        if name != _DOCUMENTED_NO_RLS_EXCEPTION and not (enabled and forced)
    ]
    assert not missing_rls, (
        f"tenant_id-bearing tables without FORCE ROW LEVEL SECURITY: {missing_rls}. "
        f"Either add the RLS policy (see the tenancy baseline migration for the shape) "
        f"or, if this is deliberately a system/infra table like `job`, add it to "
        f"_DOCUMENTED_NO_RLS_EXCEPTION with a comment explaining why."
    )


async def test_the_documented_exception_really_has_no_rls(db_available: None) -> None:
    """If `job` ever gains an RLS policy, this test's assumption (and the design
    rationale in core.ports.job_queue) needs revisiting, not silently going stale."""
    async with unscoped_session() as session:
        row = (
            await session.execute(
                text(_CATALOG_QUERY + " AND c.relname = :name"),
                {"name": _DOCUMENTED_NO_RLS_EXCEPTION},
            )
        ).one()
    _name, enabled, forced = row
    assert not (enabled and forced)
