"""``priority_weight`` -> per-class weights (the translation that was missing)."""

from __future__ import annotations

import uuid

from core.knowledge.authoring import (
    EntryFields,
    attach_source_to_workspace,
    create_source,
    publish_version,
    upsert_draft_entry,
)
from core.knowledge.publish_chunks import chunk_published_entries
from core.knowledge.retrieval.priority import class_priority_weights
from core.tenancy.seed import seed_dev_tenant


async def _published_source(tenant_id: uuid.UUID, key: str, entries: dict[str, str]) -> uuid.UUID:
    """A source whose entries span the classes given, published and chunked."""
    source = await create_source(tenant_id, key=key, name=key, class_="misc")
    for entry_key, class_ in entries.items():
        await upsert_draft_entry(
            tenant_id,
            source.id,
            entry_key,
            EntryFields(
                title=entry_key,
                body_md=f"Body of {entry_key}, long enough to chunk into something.",
                class_=class_,
                scope_key="workspace_public",
            ),
        )
    version = await publish_version(tenant_id, source.id, change_note="seed")
    await chunk_published_entries(tenant_id, source.id, version.id)
    return source.id


async def test_a_workspace_that_weights_nothing_is_left_alone(db_available: None) -> None:
    """The neutral case has to be *exactly* today's behaviour, or wiring this up would
    silently re-rank every workspace that never asked for anything."""
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"prio-n-{uuid.uuid4().hex[:8]}")
    source_id = await _published_source(tenant_id, "handbook", {"a": "rules", "b": "lore"})
    await attach_source_to_workspace(tenant_id, workspace_id, source_id, "workspace_public")

    assert await class_priority_weights(tenant_id, workspace_id) == {}


async def test_a_weight_reaches_every_class_its_source_actually_publishes(
    db_available: None,
) -> None:
    """The reason a source's own `class_` is the wrong key: a repository ingests as one
    source but its entries span all three classes -- docs/adr and CONTRIBUTING become
    `rules`, docs and tasks become `lore`, every source file becomes `misc`. Keying on the
    source would credit the weight to exactly one of them."""
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"prio-r-{uuid.uuid4().hex[:8]}")
    repo_id = await _published_source(
        tenant_id,
        "repo:thing",
        {"CONTRIBUTING.md": "rules", "docs__design.md": "lore", "src__main.rs": "misc"},
    )
    await attach_source_to_workspace(
        tenant_id, workspace_id, repo_id, "workspace_public", priority_weight=3.0
    )

    weights = await class_priority_weights(tenant_id, workspace_id)

    assert weights == {"rules": 3.0, "lore": 3.0, "misc": 3.0}


async def test_a_class_is_weighted_by_how_much_of_it_each_source_supplies(
    db_available: None,
) -> None:
    """A class's weight should reflect the material really in it. A heavily weighted source
    supplying one chunk of a class must not drag the whole class up as if it supplied all
    of it, so contributions are averaged by chunk count."""
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"prio-w-{uuid.uuid4().hex[:8]}")

    bulk = await _published_source(tenant_id, "bulk", {f"lore-{i}": "lore" for i in range(9)})
    sliver = await _published_source(tenant_id, "sliver", {"lore-x": "lore"})
    await attach_source_to_workspace(tenant_id, workspace_id, bulk, "workspace_public")
    await attach_source_to_workspace(
        tenant_id, workspace_id, sliver, "workspace_public", priority_weight=11.0
    )

    weights = await class_priority_weights(tenant_id, workspace_id)

    # 9 chunks at 1.0 and 1 chunk at 11.0 -> (9 + 11) / 10 == 2.0, not 6.0 (a flat mean of
    # the two sources) and not 11.0 (the loudest source winning outright).
    assert weights["lore"] == 2.0


async def test_a_disabled_attachment_carries_no_weight(db_available: None) -> None:
    """Detaching by disabling must remove the source's say as well as its chunks."""
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"prio-d-{uuid.uuid4().hex[:8]}")
    source_id = await _published_source(tenant_id, "loud", {"a": "misc"})
    await attach_source_to_workspace(
        tenant_id, workspace_id, source_id, "workspace_public", priority_weight=9.0, enabled=False
    )

    assert await class_priority_weights(tenant_id, workspace_id) == {}
