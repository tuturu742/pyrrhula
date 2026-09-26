"""Shared seeding helper for dense/sparse retrieval tests: inserts a published entry +
chunk directly (raw SQL, mirroring how the ingestion pipeline actually writes
chunks) with fully controllable embedding/text/scope/class, rather than going through
the real ingestion+embedding pipeline -- these tests are about the *retrieval query*,
not ingestion, and need to control exactly which vector or text lands where.

The rows are stamped with a real ``knowledge_source_version`` because that is the only
shape production ever produces: ``publish_chunks.chunk_published_entries`` chunks
published entries only, so a chunk with a NULL version cannot exist outside a fixture.
Seeding drafts here was quietly load-bearing -- it let retrieval tests pass while
retrieval had no version predicate at all, which is how superseded text stayed citable.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from core.knowledge.authoring import attach_source_to_workspace, create_source
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


def unit_vector(dim: int, dimension: int = 1024) -> list[float]:
    """A 1024-dim vector that's exactly 1.0 along axis `dim`, 0 elsewhere -- cosine
    distance to another such vector is 0 (identical) or 1 (orthogonal), so dense search
    results are fully deterministic and don't depend on any real embedding model."""
    vector = [0.0] * dimension
    vector[dim] = 1.0
    return vector


async def seed_tenant_and_source(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    await current_version(tenant_id, source.id)
    return tenant_id, source.id


async def current_version(tenant_id: uuid.UUID, source_id: uuid.UUID) -> uuid.UUID:
    """The source's current published version, created on first use.

    Not ``authoring.publish_version``: that copies draft entries into fresh published
    rows, which would leave a directly-inserted chunk pointing at the draft copy. The
    fixture writes the published shape in one step instead.
    """
    async with tenant_scope(tenant_id) as session:
        version_id = await session.scalar(
            text("SELECT current_version_id FROM knowledge_source WHERE id = :source_id"),
            {"source_id": source_id},
        )
        if version_id is not None:
            return version_id
        version_id = await session.scalar(
            text(
                "INSERT INTO knowledge_source_version "
                "(tenant_id, knowledge_source_id, version_number, content_hash) "
                "VALUES (:tenant_id, :source_id, 1, :content_hash) RETURNING id"
            ),
            {
                "tenant_id": tenant_id,
                "source_id": source_id,
                "content_hash": uuid.uuid4().hex,
            },
        )
        await session.execute(
            text("UPDATE knowledge_source SET current_version_id = :v WHERE id = :source_id"),
            {"v": version_id, "source_id": source_id},
        )
    return version_id


async def publish_next_version(tenant_id: uuid.UUID, source_id: uuid.UUID) -> uuid.UUID:
    """Supersede the source's current version with a fresh one, the way a re-analysis or
    a re-publish does: the old version's rows stay exactly where they are."""
    async with tenant_scope(tenant_id) as session:
        prior = await session.scalar(
            text("SELECT current_version_id FROM knowledge_source WHERE id = :source_id"),
            {"source_id": source_id},
        )
        next_number = await session.scalar(
            text(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM knowledge_source_version "
                "WHERE knowledge_source_id = :source_id"
            ),
            {"source_id": source_id},
        )
        version_id = await session.scalar(
            text(
                "INSERT INTO knowledge_source_version "
                "(tenant_id, knowledge_source_id, version_number, content_hash, "
                " parent_version_id) "
                "VALUES (:tenant_id, :source_id, :n, :content_hash, :parent) RETURNING id"
            ),
            {
                "tenant_id": tenant_id,
                "source_id": source_id,
                "n": next_number,
                "content_hash": uuid.uuid4().hex,
                "parent": prior,
            },
        )
        await session.execute(
            text("UPDATE knowledge_source SET current_version_id = :v WHERE id = :source_id"),
            {"v": version_id, "source_id": source_id},
        )
    return version_id


async def versions_of(tenant_id: uuid.UUID) -> frozenset[uuid.UUID]:
    """Every source's current version in this tenant -- what a workspace that had them
    all attached would resolve, and what the retrieval functions now require."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT current_version_id FROM knowledge_source "
                    "WHERE current_version_id IS NOT NULL"
                )
            )
        ).all()
    return frozenset(row[0] for row in rows)


async def attach_to_workspace(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    scope_key: str = "workspace_public",
) -> None:
    """Attach a source at "follow the current version" -- the shape every seeded and
    imported tenant has, and the one the assembler resolves versions from."""
    await attach_source_to_workspace(tenant_id, workspace_id, source_id, scope_key)


async def seed_chunk(
    tenant_id: uuid.UUID,
    knowledge_source_id: uuid.UUID,
    *,
    entry_key: str,
    body_text: str,
    class_: str,
    scope_key: str,
    embedding: list[float] | None = None,
) -> uuid.UUID:
    """Inserts a published entry + one chunk for it directly, returning the chunk id."""
    version_id = await current_version(tenant_id, knowledge_source_id)
    async with tenant_scope(tenant_id) as session:
        entry_id = (
            await session.execute(
                text(
                    "INSERT INTO knowledge_entry "
                    "(tenant_id, knowledge_source_id, version_id, entry_key, title, "
                    " body_md, class, scope_key, keys, secondary_keys, logic, "
                    " use_regex, constant, position, insertion_order) "
                    "VALUES (:tenant_id, :source_id, :version_id, :entry_key, :entry_key, "
                    " :body_text, :class_, :scope_key, '{}', '{}', 'AND', false, "
                    " false, 'before_char', 0) "
                    "RETURNING id"
                ),
                {
                    "tenant_id": tenant_id,
                    "source_id": knowledge_source_id,
                    "version_id": version_id,
                    "entry_key": entry_key,
                    "body_text": body_text,
                    "class_": class_,
                    "scope_key": scope_key,
                },
            )
        ).scalar_one()

        vector_sql = "NULL"
        params = {
            "tenant_id": tenant_id,
            "entry_id": entry_id,
            "version_id": version_id,
            "text": body_text,
            "token_count": len(body_text.split()),
            "class_": class_,
            "scope_key": scope_key,
            "content_hash": uuid.uuid4().hex,
        }
        if embedding is not None:
            vector_sql = "CAST(:vec AS vector)"
            params["vec"] = "[" + ",".join(repr(float(x)) for x in embedding) + "]"

        chunk_id = (
            await session.execute(
                text(
                    "INSERT INTO knowledge_chunk "
                    "(tenant_id, entry_id, version_id, ordinal, text, token_count, "
                    " class, scope_key, embedding, content_hash) "
                    f"VALUES (:tenant_id, :entry_id, :version_id, 0, :text, :token_count, "
                    f" :class_, :scope_key, {vector_sql}, :content_hash) "
                    "RETURNING id"
                ),
                params,
            )
        ).scalar_one()

    return chunk_id
