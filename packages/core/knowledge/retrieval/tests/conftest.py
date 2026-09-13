"""Shared seeding helper for dense/sparse retrieval tests: inserts a draft entry +
chunk directly (raw SQL, mirroring how A1.2's ingestion pipeline actually writes
chunks) with fully controllable embedding/text/scope/class, rather than going through
the real ingestion+embedding pipeline -- these tests are about the *retrieval query*,
not ingestion, and need to control exactly which vector or text lands where.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from core.knowledge.authoring import create_source
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
    return tenant_id, source.id


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
    """Inserts a draft entry + one chunk for it directly, returning the chunk id."""
    async with tenant_scope(tenant_id) as session:
        entry_id = (
            await session.execute(
                text(
                    "INSERT INTO knowledge_entry "
                    "(tenant_id, knowledge_source_id, version_id, entry_key, title, "
                    " body_md, class, scope_key, keys, secondary_keys, logic, "
                    " use_regex, constant, position, insertion_order) "
                    "VALUES (:tenant_id, :source_id, NULL, :entry_key, :entry_key, "
                    " :body_text, :class_, :scope_key, '{}', '{}', 'AND', false, "
                    " false, 'before_char', 0) "
                    "RETURNING id"
                ),
                {
                    "tenant_id": tenant_id,
                    "source_id": knowledge_source_id,
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
                    f"VALUES (:tenant_id, :entry_id, NULL, 0, :text, :token_count, "
                    f" :class_, :scope_key, {vector_sql}, :content_hash) "
                    "RETURNING id"
                ),
                params,
            )
        ).scalar_one()

    return chunk_id
