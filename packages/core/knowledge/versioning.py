"""Pin-vs-follow resolution and fork-from-version (plan §6.1, §5.4 req 5, A1.8) — how
rules iterate without breaking whatever is currently reading them.

Pin-vs-follow operates at the granularity the schema actually has: a workspace's
attachment of a source (``workspace_knowledge_attachment.version_pin``), not a
per-session pin — no session-level "which version did I start with" field exists yet
(that needs B1.4's session/checkpoint model, which doesn't exist yet either). "An active
session pins the version it started with" (plan §5.4) is achieved through this same
mechanism today: a workspace operator sets ``version_pin`` before a session starts, and
retrieval consistently reads that pinned version regardless of what gets published to the
source afterward — proven by ``test_editing_a_source_after_pinning_does_not_change_the_
resolved_version``. True per-session pinning, independent of workspace-wide pin state,
would be a schema addition for whichever B-track task actually needs it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.knowledge.authoring import EntryFields, publish_version, upsert_draft_entry
from core.knowledge.models import (
    KnowledgeEntry,
    KnowledgeSource,
    WorkspaceKnowledgeAttachment,
)
from core.tenancy.scope import tenant_scope


async def resolve_effective_version_id(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, knowledge_source_id: uuid.UUID
) -> uuid.UUID | None:
    """``NULL``/no attachment => follow the source's ``current_version_id`` (latest
    published). A set ``version_pin`` wins regardless of what's published afterward."""
    async with tenant_scope(tenant_id) as session:
        attachment = await session.scalar(
            select(WorkspaceKnowledgeAttachment).where(
                WorkspaceKnowledgeAttachment.workspace_id == workspace_id,
                WorkspaceKnowledgeAttachment.knowledge_source_id == knowledge_source_id,
            )
        )
        if attachment is not None and attachment.version_pin is not None:
            return attachment.version_pin

        source = await session.get(KnowledgeSource, knowledge_source_id)
        return source.current_version_id if source is not None else None


async def fork_source(
    tenant_id: uuid.UUID,
    from_source_id: uuid.UUID,
    from_version_id: uuid.UUID,
    new_key: str,
    new_name: str,
    *,
    created_by: uuid.UUID | None = None,
) -> KnowledgeSource:
    """Creates an independent, editable copy of ``from_source_id`` as it stood at
    ``from_version_id``, in the same tenant. The fork's first publish records provenance
    back to the *origin's* version (``parent_version_id`` crosses source boundaries —
    schema-legal, since that FK only targets ``knowledge_source_version.id``, not a
    specific source). Chunks aren't copied — copying entries only is what "an independent,
    editable copy" needs; re-deriving chunks is ingestion's job (A1.2), and A1.2's own
    content-hash skip means a forked entry's chunks cost nothing extra to (re-)embed if
    the content is unchanged from the origin.
    """
    async with tenant_scope(tenant_id) as session:
        origin_source = await session.get(KnowledgeSource, from_source_id)
        if origin_source is None:
            raise ValueError(f"no knowledge source {from_source_id} in this tenant")

        origin_entries = (
            (
                await session.execute(
                    select(KnowledgeEntry).where(KnowledgeEntry.version_id == from_version_id)
                )
            )
            .scalars()
            .all()
        )
        if not origin_entries:
            raise ValueError(f"no entries found for version {from_version_id}")

        new_source = KnowledgeSource(
            tenant_id=tenant_id,
            key=new_key,
            name=new_name,
            class_=origin_source.class_,
            owner_principal_id=created_by,
            visibility=origin_source.visibility,
        )
        session.add(new_source)
        await session.flush()
        new_source_id = new_source.id

    for entry in origin_entries:
        await upsert_draft_entry(
            tenant_id,
            new_source_id,
            entry.entry_key,
            EntryFields(
                title=entry.title,
                body_md=entry.body_md,
                class_=entry.class_,
                scope_key=entry.scope_key,
                keys=list(entry.keys),
                secondary_keys=list(entry.secondary_keys),
                logic=entry.logic,
                use_regex=entry.use_regex,
                constant=entry.constant,
                sticky=entry.sticky,
                cooldown=entry.cooldown,
                delay=entry.delay,
                trigger_pct=entry.trigger_pct,
                inclusion_group=entry.inclusion_group,
                position=entry.position,
                insertion_order=entry.insertion_order,
            ),
        )

    await publish_version(
        tenant_id,
        new_source_id,
        created_by=created_by,
        change_note=f"forked from source {from_source_id} version {from_version_id}",
        parent_version_override=from_version_id,
    )

    async with tenant_scope(tenant_id) as session:
        forked = await session.get(KnowledgeSource, new_source_id)
        assert forked is not None
        return forked
