"""T0.4's library-tenant matrix (A1.10, D13, §16.8): parameterised {tenant A, tenant B,
library} -- B reads library, B cannot read A, nobody writes library. Referenced (as "not
here yet") in ``test_filter_omission_matrix.py``'s module docstring; this is that matrix,
landed now that the library tenant exists.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.knowledge.authoring import (
    EntryFields,
    create_source,
    publish_version,
    upsert_draft_entry,
)
from core.knowledge.library import LIBRARY_TENANT_ID
from core.tenancy.scope import tenant_scope


async def _seed_source(tenant_id: uuid.UUID, key: str) -> uuid.UUID:
    source = await create_source(tenant_id, key=key, name=key, class_="rules")
    await upsert_draft_entry(
        tenant_id,
        source.id,
        "entry-a",
        EntryFields(title="Entry", body_md="body", class_="rules", scope_key="workspace_public"),
    )
    await publish_version(tenant_id, source.id)
    return source.id


async def test_tenant_b_reads_library_and_cannot_read_tenant_a(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    library_source_id = await _seed_source(LIBRARY_TENANT_ID, f"lib-{uuid.uuid4().hex[:8]}")
    await _seed_source(tenant_a, f"a-only-{uuid.uuid4().hex[:8]}")

    async with tenant_scope(tenant_b) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM knowledge_source"))).all()

    tenant_ids_visible_to_b = {row[0] for row in rows}
    assert LIBRARY_TENANT_ID in tenant_ids_visible_to_b, "B must read the library tenant's sources"
    assert tenant_a not in tenant_ids_visible_to_b, "B must never read tenant A's sources"
    assert library_source_id  # sanity: seeding produced a real id


async def test_nobody_but_the_library_tenant_can_write_a_library_row(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    library_source_id = await _seed_source(LIBRARY_TENANT_ID, f"lib-{uuid.uuid4().hex[:8]}")

    for foreign_tenant in (tenant_a, tenant_b):
        with pytest.raises(DBAPIError, match="row-level security policy"):
            async with tenant_scope(foreign_tenant) as session:
                await session.execute(
                    text("UPDATE knowledge_source SET name = 'hacked' WHERE id = :id"),
                    {"id": library_source_id},
                )

    async with tenant_scope(LIBRARY_TENANT_ID) as session:
        row = (
            await session.execute(
                text("SELECT name FROM knowledge_source WHERE id = :id"),
                {"id": library_source_id},
            )
        ).scalar_one()
    assert row != "hacked"


async def test_tenant_b_reads_library_entries_and_chunks_not_just_the_source_row(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The disjunct covers knowledge_source_version and knowledge_entry too, not just
    knowledge_source -- a shallow test that only checked the source row could pass while
    the entry/version tables (where the actual content lives) still hid everything."""
    _tenant_a, tenant_b = two_tenants

    source = await create_source(
        LIBRARY_TENANT_ID, key=f"lib-deep-{uuid.uuid4().hex[:8]}", name="Deep", class_="rules"
    )
    await upsert_draft_entry(
        LIBRARY_TENANT_ID,
        source.id,
        "deep-entry",
        EntryFields(title="Deep", body_md="body", class_="rules", scope_key="workspace_public"),
    )
    version = await publish_version(LIBRARY_TENANT_ID, source.id)

    async with tenant_scope(tenant_b) as session:
        entry_tenant_ids = (
            await session.execute(
                text(
                    "SELECT tenant_id FROM knowledge_entry WHERE knowledge_source_id = :sid "
                    "AND version_id = :vid"
                ),
                {"sid": source.id, "vid": version.id},
            )
        ).all()
        version_tenant_ids = (
            await session.execute(
                text("SELECT tenant_id FROM knowledge_source_version WHERE id = :vid"),
                {"vid": version.id},
            )
        ).all()

    assert {row[0] for row in entry_tenant_ids} == {LIBRARY_TENANT_ID}
    assert {row[0] for row in version_tenant_ids} == {LIBRARY_TENANT_ID}
