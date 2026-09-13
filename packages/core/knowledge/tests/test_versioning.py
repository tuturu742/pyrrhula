"""A1.8 acceptance criteria for pin-vs-follow resolution and fork-from-version, against a
live Postgres.
"""

from __future__ import annotations

import uuid

import pytest

from core.knowledge.authoring import (
    EntryFields,
    attach_source_to_workspace,
    create_source,
    list_version_entries,
    publish_version,
    upsert_draft_entry,
)
from core.knowledge.models import KnowledgeSourceVersion
from core.knowledge.versioning import fork_source, resolve_effective_version_id
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    return tenant_id, workspace_id, source.id


def _entry(title: str, body: str) -> EntryFields:
    return EntryFields(title=title, body_md=body, class_="rules", scope_key="workspace_public")


# ── pin vs follow ────────────────────────────────────────────────────────────────────


async def test_no_attachment_and_no_publish_resolves_to_none(db_available: None) -> None:
    tenant_id, workspace_id, source_id = await _setup("ver-none")
    assert await resolve_effective_version_id(tenant_id, workspace_id, source_id) is None


async def test_follows_latest_when_no_pin_set(db_available: None) -> None:
    tenant_id, workspace_id, source_id = await _setup("ver-follow")
    await upsert_draft_entry(tenant_id, source_id, "rule", _entry("Rule", "v1"))
    v1 = await publish_version(tenant_id, source_id)
    await attach_source_to_workspace(tenant_id, workspace_id, source_id, "workspace_public")

    assert await resolve_effective_version_id(tenant_id, workspace_id, source_id) == v1.id

    await upsert_draft_entry(tenant_id, source_id, "rule", _entry("Rule", "v2"))
    v2 = await publish_version(tenant_id, source_id)
    # No pin -> tracks the newly published version automatically.
    assert await resolve_effective_version_id(tenant_id, workspace_id, source_id) == v2.id


async def test_editing_a_source_after_pinning_does_not_change_the_resolved_version(
    db_available: None,
) -> None:
    """The pin test: a workspace/session that pinned to v1 keeps reading v1 no matter how
    many times the source gets republished afterward."""
    tenant_id, workspace_id, source_id = await _setup("ver-pin")
    await upsert_draft_entry(tenant_id, source_id, "rule", _entry("Rule", "v1"))
    v1 = await publish_version(tenant_id, source_id)
    await attach_source_to_workspace(
        tenant_id, workspace_id, source_id, "workspace_public", version_pin=v1.id
    )

    for i in range(2, 5):
        await upsert_draft_entry(tenant_id, source_id, "rule", _entry("Rule", f"v{i}"))
        await publish_version(tenant_id, source_id)

    assert await resolve_effective_version_id(tenant_id, workspace_id, source_id) == v1.id


# ── fork-from-version ─────────────────────────────────────────────────────────────────


async def test_fork_copies_entries_and_publishes_with_provenance(db_available: None) -> None:
    tenant_id, _workspace_id, source_id = await _setup("ver-fork")
    await upsert_draft_entry(tenant_id, source_id, "grappling", _entry("Grappling", "roll str"))
    await upsert_draft_entry(tenant_id, source_id, "stealth", _entry("Stealth", "roll dex"))
    origin_version = await publish_version(tenant_id, source_id)

    forked = await fork_source(
        tenant_id, source_id, origin_version.id, "core-rules-fork", "Core Rules (Fork)"
    )

    assert forked.id != source_id
    assert forked.current_version_id is not None

    forked_entries = await list_version_entries(tenant_id, forked.current_version_id)
    assert {e.entry_key for e in forked_entries} == {"grappling", "stealth"}

    # Provenance crosses the source boundary: the fork's v1 parent is the *origin's*
    # version, not None (a brand-new source would otherwise have no parent).
    async with tenant_scope(tenant_id) as session:
        forked_version = await session.get(KnowledgeSourceVersion, forked.current_version_id)
        assert forked_version is not None
        assert forked_version.parent_version_id == origin_version.id


async def test_fork_is_independently_editable_without_touching_the_origin(
    db_available: None,
) -> None:
    tenant_id, _workspace_id, source_id = await _setup("ver-fork-indep")
    await upsert_draft_entry(tenant_id, source_id, "grappling", _entry("Grappling", "roll str"))
    origin_version = await publish_version(tenant_id, source_id)

    forked = await fork_source(
        tenant_id, source_id, origin_version.id, "core-rules-fork2", "Fork 2"
    )

    # Edit only the fork's draft.
    await upsert_draft_entry(
        tenant_id, forked.id, "grappling", _entry("Grappling", "roll str + house rule")
    )
    fork_v2 = await publish_version(tenant_id, forked.id)

    origin_entries = await list_version_entries(tenant_id, origin_version.id)
    assert origin_entries[0].body_md == "roll str"  # untouched

    fork_v2_entries = await list_version_entries(tenant_id, fork_v2.id)
    assert fork_v2_entries[0].body_md == "roll str + house rule"


async def test_fork_rejects_unknown_source(db_available: None) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"ver-fork-missing-{uuid.uuid4().hex[:8]}"
    )
    with pytest.raises(ValueError):
        await fork_source(tenant_id, uuid.uuid4(), uuid.uuid4(), "new-key", "New")


async def test_fork_rejects_version_with_no_entries(db_available: None) -> None:
    tenant_id, _workspace_id, source_id = await _setup("ver-fork-empty")
    # Publish with zero draft entries -- a legal, if odd, empty version.
    empty_version = await publish_version(tenant_id, source_id)
    with pytest.raises(ValueError):
        await fork_source(tenant_id, source_id, empty_version.id, "new-key", "New")
