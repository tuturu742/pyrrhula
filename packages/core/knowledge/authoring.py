"""Knowledge authoring: create sources, edit draft entries, publish immutable versions,
attach sources to workspaces. This is the module ``api/routes/knowledge.py``
imports — never ``core.knowledge.repo``, which INV-1 (the import-graph lint) reserves
for ``core.assembler``/``core.overseer``. Authoring reads/writes are a human editing their
own tenant's content through the UI, not stored text reaching a model; INV-1 protects the
latter path, not the former, so this module talks to the tables directly rather than
funnelling through the restricted one.

Draft/publish model (see ``core.knowledge.models`` for the full rationale): entries with
``version_id IS NULL`` are the mutable draft; ``publish_version`` snapshots them into a
new, immutable ``KnowledgeSourceVersion`` row plus copies of the entries stamped with that
version's id, and leaves the original draft rows untouched for the next edit round.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select

from core.knowledge.activation import validate_regex_keys
from core.knowledge.hashing import compute_content_hash
from core.knowledge.keys import derive_keys
from core.knowledge.models import (
    KnowledgeEntry,
    KnowledgeSource,
    KnowledgeSourceVersion,
    WorkspaceKnowledgeAttachment,
)
from core.tenancy.scope import tenant_scope

# Which classes get keys derived from their titles. `rules` only, for now: a rulebook's
# sections are named after the things they govern, so their titles are what a turn calls
# them. Lore titles are proper nouns that dense search already finds, and misc is texture
# nobody looks up by name.
DERIVED_KEY_CLASSES = frozenset({"rules"})


async def create_source(
    tenant_id: uuid.UUID,
    key: str,
    name: str,
    class_: str,
    *,
    owner_principal_id: uuid.UUID | None = None,
    visibility: str = "tenant",
) -> KnowledgeSource:
    async with tenant_scope(tenant_id) as session:
        source = KnowledgeSource(
            tenant_id=tenant_id,
            key=key,
            name=name,
            class_=class_,
            owner_principal_id=owner_principal_id,
            visibility=visibility,
        )
        session.add(source)
        await session.flush()
        return source


async def list_sources(
    tenant_id: uuid.UUID, *, include_archived: bool = False
) -> list[KnowledgeSource]:
    async with tenant_scope(tenant_id) as session:
        stmt = select(KnowledgeSource).where(KnowledgeSource.tenant_id == tenant_id)
        if not include_archived:
            stmt = stmt.where(KnowledgeSource.archived_at.is_(None))
        rows = (await session.execute(stmt)).scalars()
        return list(rows)


async def archive_source(tenant_id: uuid.UUID, knowledge_source_id: uuid.UUID) -> None:
    """Soft-delete a knowledge source: stamp ``archived_at`` and drop its workspace
    attachments (a mutable table, so a real delete) so retrieval stops seeing it. The
    source's append-only version history is left intact -- purge is the superuser CLI's job."""
    async with tenant_scope(tenant_id) as session:
        source = await session.get(KnowledgeSource, knowledge_source_id)
        if source is None:
            raise ValueError(f"no knowledge source {knowledge_source_id} in this tenant")
        if source.archived_at is None:
            source.archived_at = datetime.now(UTC)
        await session.execute(
            delete(WorkspaceKnowledgeAttachment).where(
                WorkspaceKnowledgeAttachment.knowledge_source_id == knowledge_source_id
            )
        )
        await session.flush()


async def get_source(
    tenant_id: uuid.UUID, knowledge_source_id: uuid.UUID
) -> KnowledgeSource | None:
    async with tenant_scope(tenant_id) as session:
        return await session.get(KnowledgeSource, knowledge_source_id)


@dataclass
class EntryFields:
    title: str
    body_md: str
    class_: str
    scope_key: str
    keys: list[str] = field(default_factory=list)
    secondary_keys: list[str] = field(default_factory=list)
    logic: str = "AND"
    use_regex: bool = False
    constant: bool = False
    sticky: int | None = None
    cooldown: int | None = None
    delay: int | None = None
    trigger_pct: int | None = None
    inclusion_group: str | None = None
    position: str = "before_char"
    insertion_order: int = 0


async def upsert_draft_entry(
    tenant_id: uuid.UUID,
    knowledge_source_id: uuid.UUID,
    entry_key: str,
    fields: EntryFields,
) -> KnowledgeEntry:
    """Create or update the draft (``version_id IS NULL``) entry for ``entry_key``. Never
    touches a published entry — those are immutable snapshots owned by a specific version."""
    if fields.use_regex:
        validate_regex_keys(fields.keys + fields.secondary_keys)

    async with tenant_scope(tenant_id) as session:
        source = await session.get(KnowledgeSource, knowledge_source_id)
        if source is None:
            raise ValueError(f"no knowledge source {knowledge_source_id} in this tenant")

        existing = await session.scalar(
            select(KnowledgeEntry).where(
                KnowledgeEntry.knowledge_source_id == knowledge_source_id,
                KnowledgeEntry.entry_key == entry_key,
                KnowledgeEntry.version_id.is_(None),
            )
        )
        if existing is not None:
            existing.title = fields.title
            existing.body_md = fields.body_md
            existing.class_ = fields.class_
            existing.scope_key = fields.scope_key
            # Keys written to something else are the author's from now on. Clearing them
            # is not that: it is "none, thank you", and it has to stick or the next
            # publish would derive them again (core.knowledge.keys).
            if fields.keys and list(fields.keys) != list(existing.keys):
                existing.keys_derived = False
            existing.keys = fields.keys
            existing.secondary_keys = fields.secondary_keys
            existing.logic = fields.logic
            existing.use_regex = fields.use_regex
            existing.constant = fields.constant
            existing.sticky = fields.sticky
            existing.cooldown = fields.cooldown
            existing.delay = fields.delay
            existing.trigger_pct = fields.trigger_pct
            existing.inclusion_group = fields.inclusion_group
            existing.position = fields.position
            existing.insertion_order = fields.insertion_order
            await session.flush()
            return existing

        entry = KnowledgeEntry(
            tenant_id=tenant_id,
            knowledge_source_id=knowledge_source_id,
            version_id=None,
            entry_key=entry_key,
            title=fields.title,
            body_md=fields.body_md,
            class_=fields.class_,
            scope_key=fields.scope_key,
            keys=fields.keys,
            secondary_keys=fields.secondary_keys,
            logic=fields.logic,
            use_regex=fields.use_regex,
            constant=fields.constant,
            sticky=fields.sticky,
            cooldown=fields.cooldown,
            delay=fields.delay,
            trigger_pct=fields.trigger_pct,
            inclusion_group=fields.inclusion_group,
            position=fields.position,
            insertion_order=fields.insertion_order,
        )
        session.add(entry)
        await session.flush()
        return entry


async def list_draft_entries(
    tenant_id: uuid.UUID, knowledge_source_id: uuid.UUID
) -> list[KnowledgeEntry]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(KnowledgeEntry).where(
                    KnowledgeEntry.knowledge_source_id == knowledge_source_id,
                    KnowledgeEntry.version_id.is_(None),
                )
            )
        ).scalars()
        return list(rows)


async def list_version_entries(tenant_id: uuid.UUID, version_id: uuid.UUID) -> list[KnowledgeEntry]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(KnowledgeEntry).where(KnowledgeEntry.version_id == version_id)
            )
        ).scalars()
        return list(rows)


async def list_versions(
    tenant_id: uuid.UUID, knowledge_source_id: uuid.UUID
) -> list[KnowledgeSourceVersion]:
    """The version history panel: every published version of a source, newest first.
    ``knowledge_source_version`` had no companion "list all" read before this --
    the other endpoints only ever needed one version at a time (a specific id, or
    the pair a diff compares) -- so this is new, not a duplicate of something else."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(KnowledgeSourceVersion)
                .where(KnowledgeSourceVersion.knowledge_source_id == knowledge_source_id)
                .order_by(KnowledgeSourceVersion.version_number.desc())
            )
        ).scalars()
        return list(rows)


async def delete_draft_entry(
    tenant_id: uuid.UUID, knowledge_source_id: uuid.UUID, entry_key: str
) -> None:
    """Removes an entry from the draft (this is what makes "removed" a reachable
    diff outcome — publishing simply never sees a deleted draft row again). Only the
    draft; a published entry is an immutable historical snapshot and is never deleted —
    ``knowledge_entry`` isn't append-only at the grant level (only
    ``knowledge_source_version`` is), but publish_version's own logic is the only thing
    that's ever supposed to write a non-NULL ``version_id`` row, and this function
    deliberately can't touch those."""
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            delete(KnowledgeEntry).where(
                KnowledgeEntry.knowledge_source_id == knowledge_source_id,
                KnowledgeEntry.entry_key == entry_key,
                KnowledgeEntry.version_id.is_(None),
            )
        )


async def publish_version(
    tenant_id: uuid.UUID,
    knowledge_source_id: uuid.UUID,
    *,
    created_by: uuid.UUID | None = None,
    change_note: str | None = None,
    parent_version_override: uuid.UUID | None = None,
    ai_assisted: bool = False,
    chunk: bool = True,
) -> KnowledgeSourceVersion:
    """Snapshot the current draft into a new immutable version, and chunk it for
    retrieval. ``chunk=False`` is for the one caller that chunks on its own terms (a
    character card's lore entry is one activation unit and must stay one chunk). The version row is
    INSERTed once, fully formed (content_hash included) — it is never created empty and
    updated later, since the app role has no UPDATE grant on ``knowledge_source_version``
    (CLAUDE.md rule 5).

    ``parent_version_override`` : normally a new version's parent is whatever this
    source's own ``current_version_id`` already was — but a freshly forked source has no
    prior version of its own yet, and its first publish needs to record provenance
    pointing at the *origin* source's version it was forked from instead. See
    ``core.knowledge.versioning.fork_source``.
    """
    async with tenant_scope(tenant_id) as session:
        source = await session.get(KnowledgeSource, knowledge_source_id)
        if source is None:
            raise ValueError(f"no knowledge source {knowledge_source_id} in this tenant")

        draft_entries = (
            (
                await session.execute(
                    select(KnowledgeEntry).where(
                        KnowledgeEntry.knowledge_source_id == knowledge_source_id,
                        KnowledgeEntry.version_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

        # An entry with no activation keys answers only to what a turn resembles, never
        # to what it names -- and nothing that ingests a document writes keys, so a
        # five-hundred-section handbook arrives with the keyed list empty. Fill them from
        # the titles that are already there, once, into the draft, so the author sees them
        # in the editor and can change or clear them (core.knowledge.keys).
        derived_entries = 0
        for draft in draft_entries:
            if draft.class_ in DERIVED_KEY_CLASSES and not draft.keys and not draft.keys_derived:
                derived = derive_keys(draft.title)
                if derived:
                    draft.keys = derived
                    draft.keys_derived = True
                    derived_entries += 1
        if derived_entries:
            await session.flush()

        content_hash = compute_content_hash(list(draft_entries))
        prior_max = await session.scalar(
            select(KnowledgeSourceVersion.version_number)
            .where(KnowledgeSourceVersion.knowledge_source_id == knowledge_source_id)
            .order_by(KnowledgeSourceVersion.version_number.desc())
            .limit(1)
        )
        version = KnowledgeSourceVersion(
            tenant_id=tenant_id,
            knowledge_source_id=knowledge_source_id,
            version_number=(prior_max or 0) + 1,
            content_hash=content_hash,
            parent_version_id=parent_version_override
            if parent_version_override is not None
            else source.current_version_id,
            created_by=created_by,
            change_note=change_note,
            ai_assisted=ai_assisted,
        )
        session.add(version)
        await session.flush()

        for draft in draft_entries:
            session.add(
                KnowledgeEntry(
                    tenant_id=tenant_id,
                    knowledge_source_id=knowledge_source_id,
                    version_id=version.id,
                    entry_key=draft.entry_key,
                    title=draft.title,
                    body_md=draft.body_md,
                    class_=draft.class_,
                    scope_key=draft.scope_key,
                    keys=list(draft.keys),
                    keys_derived=draft.keys_derived,
                    secondary_keys=list(draft.secondary_keys),
                    logic=draft.logic,
                    use_regex=draft.use_regex,
                    constant=draft.constant,
                    sticky=draft.sticky,
                    cooldown=draft.cooldown,
                    delay=draft.delay,
                    trigger_pct=draft.trigger_pct,
                    inclusion_group=draft.inclusion_group,
                    position=draft.position,
                    insertion_order=draft.insertion_order,
                )
            )

        source.current_version_id = version.id
        await session.flush()

    # Retrieval reads knowledge_chunk, never knowledge_entry -- so a version that is
    # published but not chunked is visible in the UI and absent from every assembled
    # context. That was the state of every entry authored in the product: only the
    # ingestion, repo and bundle-import paths chunked, and this one, the path the editor
    # uses, did not. Chunking here makes "published" and "retrievable" one step; the
    # caller enqueues the embedding job, which core cannot do itself.
    if chunk:
        from core.knowledge.publish_chunks import chunk_published_entries

        await chunk_published_entries(tenant_id, knowledge_source_id, version.id)
    return version


async def attach_source_to_workspace(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    knowledge_source_id: uuid.UUID,
    scope_key: str,
    *,
    priority_weight: float = 1.0,
    version_pin: uuid.UUID | None = None,
    enabled: bool = True,
) -> WorkspaceKnowledgeAttachment:
    async with tenant_scope(tenant_id) as session:
        source = await session.get(KnowledgeSource, knowledge_source_id)
        if source is None:
            raise ValueError(f"no knowledge source {knowledge_source_id} in this tenant")

        existing = await session.scalar(
            select(WorkspaceKnowledgeAttachment).where(
                WorkspaceKnowledgeAttachment.workspace_id == workspace_id,
                WorkspaceKnowledgeAttachment.knowledge_source_id == knowledge_source_id,
            )
        )
        if existing is not None:
            existing.scope_key = scope_key
            existing.priority_weight = Decimal(str(priority_weight))
            existing.version_pin = version_pin
            existing.enabled = enabled
            await session.flush()
            return existing

        attachment = WorkspaceKnowledgeAttachment(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            knowledge_source_id=knowledge_source_id,
            scope_key=scope_key,
            priority_weight=Decimal(str(priority_weight)),
            version_pin=version_pin,
            enabled=enabled,
        )
        session.add(attachment)
        await session.flush()
        return attachment


async def list_workspace_attachments(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> list[WorkspaceKnowledgeAttachment]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(WorkspaceKnowledgeAttachment).where(
                    WorkspaceKnowledgeAttachment.workspace_id == workspace_id
                )
            )
        ).scalars()
        return list(rows)


async def list_source_attachments(
    tenant_id: uuid.UUID, knowledge_source_id: uuid.UUID
) -> list[WorkspaceKnowledgeAttachment]:
    """The other half of ``list_workspace_attachments``' query direction -- the source
    detail page needs "which workspaces is *this source* attached to," not "which sources
    does *this workspace* have," and neither read is a filtered view of the other's
    result set (both are real, independent queries against the same join table)."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(WorkspaceKnowledgeAttachment).where(
                    WorkspaceKnowledgeAttachment.knowledge_source_id == knowledge_source_id
                )
            )
        ).scalars()
        return list(rows)
