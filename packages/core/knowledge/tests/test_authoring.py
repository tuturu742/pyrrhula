"""Acceptance criteria, exercised against a live Postgres."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

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

    # Published means retrievable: retrieval reads knowledge_chunk, and a version whose
    # entries were never chunked is visible in the UI and absent from every context.
    async with tenant_scope(tenant_id) as session:
        chunked = (
            (
                await session.execute(
                    text(
                        "SELECT e.entry_key FROM knowledge_chunk c "
                        "JOIN knowledge_entry e ON e.id = c.entry_id WHERE c.version_id = :v"
                    ),
                    {"v": version.id},
                )
            )
            .scalars()
            .all()
        )
    assert set(chunked) == {"grappling", "stealth"}


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


# ── keys derived from titles at publish ─────────────────────────────────────────────


async def _published_entry(tenant_id: uuid.UUID, version_id: uuid.UUID, key: str):  # noqa: ANN202
    entries = await list_version_entries(tenant_id, version_id)
    return next(e for e in entries if e.entry_key == key)


async def test_publishing_a_rules_source_fills_in_keys_from_the_titles(
    db_available: None,
) -> None:
    """Nothing that ingests a document writes activation keys, so a handbook arrives with
    the keyed list empty and retrieval left guessing from prose. The titles are already
    there."""
    tenant_id, _ws = await _setup("kn-derive")
    source = await create_source(tenant_id, key="rulebook", name="Rulebook", class_="rules")
    for entry_key, title in (
        ("goblin", "Goblin"),
        ("spider", "Spider, Giant Crab"),
        ("intro", "What is This?"),
    ):
        await upsert_draft_entry(
            tenant_id,
            source.id,
            entry_key,
            EntryFields(title=title, body_md="text", class_="rules", scope_key="workspace_public"),
        )

    version = await publish_version(tenant_id, source.id)

    assert (await _published_entry(tenant_id, version.id, "goblin")).keys == ["Goblin"]
    assert (await _published_entry(tenant_id, version.id, "spider")).keys == [
        "Spider, Giant Crab",
        "Spider",
        "Giant Crab",
    ]
    # A title that names the document rather than a thing in it derives nothing.
    assert (await _published_entry(tenant_id, version.id, "intro")).keys == []

    # Written to the draft, so the author sees them in the editor and can change them.
    drafts = {e.entry_key: e for e in await list_draft_entries(tenant_id, source.id)}
    assert drafts["goblin"].keys == ["Goblin"]
    assert drafts["goblin"].keys_derived is True
    assert drafts["intro"].keys_derived is False


async def test_only_rules_entries_get_derived_keys(db_available: None) -> None:
    """Lore titles are proper nouns dense search already finds; misc is texture nobody
    looks up by name."""
    tenant_id, _ws = await _setup("kn-derive-class")
    source = await create_source(tenant_id, key="lorebook", name="Lorebook", class_="lore")
    await upsert_draft_entry(
        tenant_id,
        source.id,
        "vale",
        EntryFields(
            title="Karsh Vale", body_md="text", class_="lore", scope_key="workspace_public"
        ),
    )
    version = await publish_version(tenant_id, source.id)
    assert (await _published_entry(tenant_id, version.id, "vale")).keys == []


async def test_keys_an_author_wrote_are_never_replaced(db_available: None) -> None:
    tenant_id, _ws = await _setup("kn-derive-authored")
    source = await create_source(tenant_id, key="rulebook", name="Rulebook", class_="rules")
    await upsert_draft_entry(
        tenant_id,
        source.id,
        "goblin",
        EntryFields(
            title="Goblin",
            body_md="text",
            class_="rules",
            scope_key="workspace_public",
            keys=["goblinoid", "hobgoblin"],
        ),
    )
    version = await publish_version(tenant_id, source.id)
    entry = await _published_entry(tenant_id, version.id, "goblin")
    assert entry.keys == ["goblinoid", "hobgoblin"]
    assert entry.keys_derived is False


async def test_clearing_derived_keys_means_none_and_stays_that_way(db_available: None) -> None:
    """The marker is what makes "this entry deliberately has no keys" expressible. Without
    it, clearing them would be undone by the next publish."""
    tenant_id, _ws = await _setup("kn-derive-cleared")
    source = await create_source(tenant_id, key="rulebook", name="Rulebook", class_="rules")
    fields = EntryFields(
        title="Goblin", body_md="text", class_="rules", scope_key="workspace_public"
    )
    await upsert_draft_entry(tenant_id, source.id, "goblin", fields)
    await publish_version(tenant_id, source.id)

    # The author clears them.
    await upsert_draft_entry(tenant_id, source.id, "goblin", fields)
    version = await publish_version(tenant_id, source.id)

    assert (await _published_entry(tenant_id, version.id, "goblin")).keys == []
    drafts = {e.entry_key: e for e in await list_draft_entries(tenant_id, source.id)}
    assert drafts["goblin"].keys_derived is True  # offered once, declined


async def test_editing_derived_keys_hands_them_to_the_author(db_available: None) -> None:
    tenant_id, _ws = await _setup("kn-derive-edited")
    source = await create_source(tenant_id, key="rulebook", name="Rulebook", class_="rules")
    await upsert_draft_entry(
        tenant_id,
        source.id,
        "goblin",
        EntryFields(title="Goblin", body_md="text", class_="rules", scope_key="workspace_public"),
    )
    await publish_version(tenant_id, source.id)

    await upsert_draft_entry(
        tenant_id,
        source.id,
        "goblin",
        EntryFields(
            title="Goblin",
            body_md="text",
            class_="rules",
            scope_key="workspace_public",
            keys=["Goblin", "goblins", "gobbo"],
        ),
    )
    drafts = {e.entry_key: e for e in await list_draft_entries(tenant_id, source.id)}
    assert drafts["goblin"].keys == ["Goblin", "goblins", "gobbo"]
    assert drafts["goblin"].keys_derived is False
