"""A1.1 acceptance criteria, exercised against a live Postgres."""

from __future__ import annotations

import uuid

import pytest

from core.knowledge.authoring import (
    EntryFields,
    attach_source_to_workspace,
    create_source,
    delete_draft_entry,
    list_draft_entries,
    list_sources,
    list_version_entries,
    list_workspace_attachments,
    publish_version,
    upsert_draft_entry,
)
from core.knowledge.hashing import compute_content_hash
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    return tenant_id, workspace_id


async def test_create_source_and_list(db_available: None) -> None:
    tenant_id, _ws = await _setup("kn-create")
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    assert source.current_version_id is None

    sources = await list_sources(tenant_id)
    assert {s.id for s in sources} == {source.id}


async def test_add_entries_then_publish_produces_reproducible_hash(db_available: None) -> None:
    tenant_id, _ws = await _setup("kn-publish")
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
    await upsert_draft_entry(
        tenant_id,
        source.id,
        "stealth",
        EntryFields(
            title="Stealth",
            body_md="Roll 1d20+DEX.",
            class_="rules",
            scope_key="workspace_public",
        ),
    )

    draft = await list_draft_entries(tenant_id, source.id)
    assert {e.entry_key for e in draft} == {"grappling", "stealth"}

    version = await publish_version(tenant_id, source.id, change_note="initial publish")
    assert version.version_number == 1
    assert version.parent_version_id is None

    published = await list_version_entries(tenant_id, version.id)
    assert {e.entry_key for e in published} == {"grappling", "stealth"}
    assert version.content_hash == compute_content_hash(published)

    # The draft rows are untouched — still there, still editable, for the next round.
    draft_after_publish = await list_draft_entries(tenant_id, source.id)
    assert {e.entry_key for e in draft_after_publish} == {"grappling", "stealth"}


async def test_republish_without_changes_is_reproducible_and_chains_parent(
    db_available: None,
) -> None:
    tenant_id, _ws = await _setup("kn-rechain")
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
    v1 = await publish_version(tenant_id, source.id)
    v2 = await publish_version(tenant_id, source.id)

    assert v2.version_number == 2
    assert v2.parent_version_id == v1.id
    # Unchanged entries -> identical content_hash across versions.
    assert v2.content_hash == v1.content_hash


async def test_attach_same_source_to_two_workspaces_with_different_settings(
    db_available: None,
) -> None:
    tenant_id, workspace_a = await _setup("kn-attach")
    async with tenant_scope(tenant_id) as session:
        ws_b = Workspace(tenant_id=tenant_id, key="second", name="Second Workspace")
        session.add(ws_b)
        await session.flush()
        workspace_b = ws_b.id

    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")

    attachment_a = await attach_source_to_workspace(
        tenant_id, workspace_a, source.id, "workspace_public", priority_weight=0.75
    )
    attachment_b = await attach_source_to_workspace(
        tenant_id, workspace_b, source.id, "faction_thieves", priority_weight=0.25
    )

    assert attachment_a.scope_key == "workspace_public"
    assert float(attachment_a.priority_weight) == 0.75
    assert attachment_b.scope_key == "faction_thieves"
    assert float(attachment_b.priority_weight) == 0.25

    attachments_a = await list_workspace_attachments(tenant_id, workspace_a)
    attachments_b = await list_workspace_attachments(tenant_id, workspace_b)
    assert {a.id for a in attachments_a} == {attachment_a.id}
    assert {a.id for a in attachments_b} == {attachment_b.id}


async def test_upsert_draft_entry_updates_in_place_not_duplicated(db_available: None) -> None:
    tenant_id, _ws = await _setup("kn-upsert")
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")

    await upsert_draft_entry(
        tenant_id,
        source.id,
        "grappling",
        EntryFields(title="Grappling", body_md="v1", class_="rules", scope_key="workspace_public"),
    )
    await upsert_draft_entry(
        tenant_id,
        source.id,
        "grappling",
        EntryFields(title="Grappling", body_md="v2", class_="rules", scope_key="workspace_public"),
    )

    draft = await list_draft_entries(tenant_id, source.id)
    assert len(draft) == 1
    assert draft[0].body_md == "v2"


async def test_upsert_draft_entry_rejects_unknown_source(db_available: None) -> None:
    tenant_id, _ws = await _setup("kn-missing-source")

    with pytest.raises(ValueError):
        await upsert_draft_entry(
            tenant_id,
            uuid.uuid4(),
            "grappling",
            EntryFields(title="x", body_md="y", class_="rules", scope_key="workspace_public"),
        )


async def test_upsert_draft_entry_rejects_invalid_regex_key_at_save(
    db_available: None,
) -> None:
    """'compile-checked at save' -- an author saving a broken regex key finds out
    immediately, not the first time the entry silently never activates."""
    from core.knowledge.activation import UnsafeRegexError

    tenant_id, _ws = await _setup("kn-bad-regex")
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")

    with pytest.raises(UnsafeRegexError):
        await upsert_draft_entry(
            tenant_id,
            source.id,
            "broken",
            EntryFields(
                title="Broken",
                body_md="x",
                class_="rules",
                scope_key="workspace_public",
                keys=["("],
                use_regex=True,
            ),
        )


async def test_delete_draft_entry_removes_it_from_the_draft(db_available: None) -> None:
    tenant_id, _ws = await _setup("kn-delete")
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    await upsert_draft_entry(
        tenant_id,
        source.id,
        "grappling",
        EntryFields(title="Grappling", body_md="x", class_="rules", scope_key="workspace_public"),
    )

    await delete_draft_entry(tenant_id, source.id, "grappling")

    draft = await list_draft_entries(tenant_id, source.id)
    assert draft == []


async def test_delete_draft_entry_does_not_touch_published_entries(db_available: None) -> None:
    tenant_id, _ws = await _setup("kn-delete-published")
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    await upsert_draft_entry(
        tenant_id,
        source.id,
        "grappling",
        EntryFields(title="Grappling", body_md="x", class_="rules", scope_key="workspace_public"),
    )
    version = await publish_version(tenant_id, source.id)

    await delete_draft_entry(tenant_id, source.id, "grappling")

    published = await list_version_entries(tenant_id, version.id)
    assert len(published) == 1


async def test_delete_draft_entry_on_unknown_key_is_a_no_op(db_available: None) -> None:
    tenant_id, _ws = await _setup("kn-delete-missing")
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    await delete_draft_entry(tenant_id, source.id, "never-existed")  # does not raise
