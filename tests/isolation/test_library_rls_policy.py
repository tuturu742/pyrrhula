"""catalog-based proof that the library-tenant RLS exception is exactly what
it claims to be -- one named disjunct, on exactly the four documented tables, nowhere
else. Complements ``test_rls_catalog.py``'s "every tenant_id table has forced RLS" with
"and this specific, narrow exception is the only place the library uuid appears."
"""

from __future__ import annotations

from sqlalchemy import text

from core.knowledge.library import LIBRARY_TENANT_ID
from core.tenancy.scope import unscoped_session

_EXPECTED_LIBRARY_READABLE_TABLES = {
    "knowledge_source",
    "knowledge_source_version",
    "knowledge_entry",
    "knowledge_chunk",
}

_POLICY_CATALOG_QUERY = """
    SELECT tablename, qual, with_check
    FROM pg_policies
    WHERE schemaname = 'public' AND policyname = 'tenant_isolation'
"""


async def test_library_uuid_appears_in_exactly_the_documented_policies(
    db_available: None,
) -> None:
    async with unscoped_session() as session:
        rows = (await session.execute(text(_POLICY_CATALOG_QUERY))).all()

    library_uuid = str(LIBRARY_TENANT_ID)
    tables_with_library_disjunct = {
        tablename for tablename, qual, _with_check in rows if library_uuid in (qual or "")
    }

    assert tables_with_library_disjunct == _EXPECTED_LIBRARY_READABLE_TABLES, (
        f"expected the library-tenant RLS exception on exactly "
        f"{sorted(_EXPECTED_LIBRARY_READABLE_TABLES)}, found it on "
        f"{sorted(tables_with_library_disjunct)}. A table gaining (or losing) this "
        f"disjunct outside a deliberate, reviewed change to the "
        f"6b3e9f2d1a47_library_tenant migration is exactly the drift this test exists "
        f"to catch."
    )


async def test_library_uuid_never_appears_in_any_with_check_clause(
    db_available: None,
) -> None:
    """The exception is read-only by construction: WITH CHECK must never grant a foreign
    tenant a way to write a library-owned row, even on the four tables that can read one."""
    async with unscoped_session() as session:
        rows = (await session.execute(text(_POLICY_CATALOG_QUERY))).all()

    library_uuid = str(LIBRARY_TENANT_ID)
    with_check_leaks = [
        tablename for tablename, _qual, with_check in rows if library_uuid in (with_check or "")
    ]
    assert not with_check_leaks, (
        f"library uuid found in a WITH CHECK clause on {with_check_leaks} -- this would "
        f"let a foreign tenant write a row claiming to belong to the library tenant"
    )


async def test_library_tenant_row_is_flagged_and_has_the_well_known_id(
    db_available: None,
) -> None:
    async with unscoped_session() as session:
        row = (
            await session.execute(
                text("SELECT is_library FROM tenant WHERE id = :id"),
                {"id": LIBRARY_TENANT_ID},
            )
        ).one_or_none()

    assert row is not None, "expected the library tenant row to exist by A1.10"
    assert row[0] is True
