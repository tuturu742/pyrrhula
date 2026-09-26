"""Filter-omission coverage for the five knowledge tables: ``knowledge_source``,
``knowledge_source_version``, ``knowledge_entry``, ``knowledge_chunk``,
``workspace_knowledge_attachment``. Same pattern as ``test_filter_omission_matrix.py`` —
a raw query with no ``WHERE tenant_id = ...`` clause, scoped to tenant A, must never
surface tenant B's rows.

Since A1.10, the first three of those five tables have one *documented* exception to
"only my own tenant_id comes back": the library tenant's rows are visible to every
tenant's scoped session by design (see ``core.knowledge.library``,
``tests/isolation/test_library_matrix.py``). Those tests below assert the still-true,
still-load-bearing half of the guarantee (tenant B's rows never leak) plus the narrower
"nothing outside {mine, the library} ever appears" rather than a strict single-tenant
equality, which A1.10 makes provably false by design, not by accident.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text

from core.knowledge.authoring import (
    EntryFields,
    attach_source_to_workspace,
    create_source,
    publish_version,
    upsert_draft_entry,
)
from core.knowledge.library import LIBRARY_TENANT_ID
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope


async def _seed_full_chain(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    await upsert_draft_entry(
        tenant_id,
        source.id,
        "grappling",
        EntryFields(
            title="Grappling",
            body_md="Roll 1d20+STR.",
            class_="rules",
            scope_key="workspace_public",
        ),
    )
    version = await publish_version(tenant_id, source.id)
    await attach_source_to_workspace(
        tenant_id, workspace_id, source.id, scope_key="workspace_public"
    )

    async with tenant_scope(tenant_id) as session:
        entry_id = (
            await session.execute(
                text(
                    "SELECT id FROM knowledge_entry WHERE knowledge_source_id = :sid "
                    "AND version_id = :vid"
                ),
                {"sid": source.id, "vid": version.id},
            )
        ).scalar_one()
        vector_literal = "[" + ",".join(["0.1"] * 1024) + "]"
        await session.execute(
            text(
                "INSERT INTO knowledge_chunk "
                "(tenant_id, entry_id, version_id, ordinal, text, token_count, class, "
                " scope_key, embedding, content_hash) "
                "VALUES (:tenant_id, :entry_id, :version_id, 0, 'Roll 1d20+STR.', 4, "
                " 'rules', 'workspace_public', CAST(:vec AS vector), 'deadbeef')"
            ),
            {
                "tenant_id": tenant_id,
                "entry_id": entry_id,
                "version_id": version.id,
                "vec": vector_literal,
            },
        )


async def _seed_both_tenants(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    for tenant_id in two_tenants:
        async with tenant_scope(tenant_id) as session:
            workspace_id = (
                await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
            ).scalar_one()
        await _seed_full_chain(tenant_id, workspace_id)


async def test_knowledge_source_filter_omission(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, tenant_b = two_tenants
    await _seed_both_tenants(two_tenants)

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM knowledge_source"))).all()
    tenant_ids = {row[0] for row in rows}
    # the library tenant's rows are the one documented, intentional exception to
    # "only my own tenant_id" -- see tests/isolation/test_library_matrix.py.
    assert tenant_ids <= {tenant_a, LIBRARY_TENANT_ID}
    assert tenant_b not in tenant_ids


async def test_knowledge_source_version_filter_omission(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    await _seed_both_tenants(two_tenants)

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM knowledge_source_version"))).all()
    tenant_ids = {row[0] for row in rows}
    assert tenant_ids <= {tenant_a, LIBRARY_TENANT_ID}
    assert tenant_b not in tenant_ids


async def test_knowledge_entry_filter_omission(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, tenant_b = two_tenants
    await _seed_both_tenants(two_tenants)

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM knowledge_entry"))).all()
    tenant_ids = {row[0] for row in rows}
    assert tenant_ids <= {tenant_a, LIBRARY_TENANT_ID}
    assert tenant_b not in tenant_ids


async def test_knowledge_chunk_filter_omission(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _tenant_b = two_tenants
    await _seed_both_tenants(two_tenants)

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM knowledge_chunk"))).all()
    assert {row[0] for row in rows} == {tenant_a}


async def test_workspace_knowledge_attachment_filter_omission(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    await _seed_both_tenants(two_tenants)

    async with tenant_scope(tenant_a) as session:
        rows = (
            await session.execute(text("SELECT tenant_id FROM workspace_knowledge_attachment"))
        ).all()
    assert {row[0] for row in rows} == {tenant_a}
