"""Acceptance criteria for entry-level diffing, against a live Postgres."""

from __future__ import annotations

import uuid

from core.knowledge.authoring import (
    EntryFields,
    create_source,
    delete_draft_entry,
    publish_version,
    upsert_draft_entry,
)
from core.knowledge.diff import diff_versions
from core.tenancy.seed import seed_dev_tenant


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    return tenant_id, source.id


def _entry(title: str, body: str) -> EntryFields:
    return EntryFields(title=title, body_md=body, class_="rules", scope_key="workspace_public")


async def test_diff_across_several_versions_returns_exact_expected_sets(
    db_available: None,
) -> None:
    tenant_id, source_id = await _setup("diff-multi")

    # v1: grappling, stealth
    await upsert_draft_entry(tenant_id, source_id, "grappling", _entry("Grappling", "v1 body"))
    await upsert_draft_entry(tenant_id, source_id, "stealth", _entry("Stealth", "stealth body"))
    v1 = await publish_version(tenant_id, source_id)

    # v2: grappling changes, stealth removed, shoving added
    await upsert_draft_entry(tenant_id, source_id, "grappling", _entry("Grappling", "v2 body"))
    await delete_draft_entry(tenant_id, source_id, "stealth")
    await upsert_draft_entry(tenant_id, source_id, "shoving", _entry("Shoving", "shove body"))
    v2 = await publish_version(tenant_id, source_id)

    diff = await diff_versions(tenant_id, v1.id, v2.id)
    assert diff.added == ["shoving"]
    assert diff.removed == ["stealth"]
    assert [c.entry_key for c in diff.changed] == ["grappling"]


async def test_unchanged_entry_across_versions_is_not_reported_as_changed(
    db_available: None,
) -> None:
    tenant_id, source_id = await _setup("diff-unchanged")
    await upsert_draft_entry(tenant_id, source_id, "grappling", _entry("Grappling", "same body"))
    v1 = await publish_version(tenant_id, source_id)
    v2 = await publish_version(tenant_id, source_id)  # republish, nothing edited

    diff = await diff_versions(tenant_id, v1.id, v2.id)
    assert diff.added == diff.removed == diff.changed == []


async def test_changed_entry_carries_a_readable_text_diff(db_available: None) -> None:
    tenant_id, source_id = await _setup("diff-text")
    await upsert_draft_entry(tenant_id, source_id, "grappling", _entry("Grappling", "old line"))
    v1 = await publish_version(tenant_id, source_id)
    await upsert_draft_entry(tenant_id, source_id, "grappling", _entry("Grappling", "new line"))
    v2 = await publish_version(tenant_id, source_id)

    diff = await diff_versions(tenant_id, v1.id, v2.id)
    assert len(diff.changed) == 1
    text_diff = diff.changed[0].text_diff
    assert "-old line" in text_diff
    assert "+new line" in text_diff


async def test_diff_of_a_version_against_itself_is_empty(db_available: None) -> None:
    tenant_id, source_id = await _setup("diff-self")
    await upsert_draft_entry(tenant_id, source_id, "grappling", _entry("Grappling", "body"))
    v1 = await publish_version(tenant_id, source_id)

    diff = await diff_versions(tenant_id, v1.id, v1.id)
    assert diff.added == diff.removed == diff.changed == []
