"""core.knowledge.repo — the INV-1-restricted read path (only core.assembler/
core.overseer may import this module in production code; tests are exempt, same as
INV-1's own lint)."""

from __future__ import annotations

import uuid

from core.knowledge.authoring import EntryFields, create_source, publish_version, upsert_draft_entry
from core.knowledge.repo import get_current_version_id, list_published_entries
from core.tenancy.seed import seed_dev_tenant


async def test_get_current_version_id_and_list_published_entries(db_available: None) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"kn-repo-{uuid.uuid4().hex[:8]}"
    )
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    assert await get_current_version_id(tenant_id, source.id) is None

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

    assert await get_current_version_id(tenant_id, source.id) == version.id
    entries = await list_published_entries(tenant_id, version.id)
    assert {e.entry_key for e in entries} == {"grappling"}


async def test_get_current_version_id_returns_none_for_unknown_source(db_available: None) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"kn-repo-missing-{uuid.uuid4().hex[:8]}"
    )
    assert await get_current_version_id(tenant_id, uuid.uuid4()) is None
