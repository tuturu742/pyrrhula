import uuid

import pytest
from sqlalchemy import text

from adapters.vector.pgvector.store import PgVectorStore
from core.ports.vector_store import VectorStore
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def _insert(tenant_id: uuid.UUID, scope_key: str, class_: str, vec: list[float]) -> None:
    vector_literal = "[" + ",".join(str(v) for v in vec) + "]"
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO vector_store_item (tenant_id, scope_key, class, embedding, payload) "
                "VALUES (:tenant_id, :scope_key, :class_, CAST(:vec AS vector), '{}'::jsonb)"
            ),
            {
                "tenant_id": tenant_id,
                "scope_key": scope_key,
                "class_": class_,
                "vec": vector_literal,
            },
        )


async def test_search_requires_nonempty_scope_keys(db_available: None) -> None:
    store: VectorStore = PgVectorStore()
    with pytest.raises(ValueError, match="scope_keys"):
        await store.search(
            tenant_id=uuid.uuid4(),
            scope_keys=frozenset(),
            class_="lore",
            query_embedding=[0.0] * 8,
            k=5,
        )


async def test_search_returns_only_matching_scope_and_class(db_available: None) -> None:
    tenant_id, _, _ = await seed_dev_tenant(slug=f"vec-{uuid.uuid4().hex[:8]}")
    store: VectorStore = PgVectorStore()

    await _insert(tenant_id, "workspace_public", "lore", [1.0, 0, 0, 0, 0, 0, 0, 0])
    await _insert(tenant_id, "facilitator_only", "lore", [1.0, 0, 0, 0, 0, 0, 0, 0])
    await _insert(tenant_id, "workspace_public", "rules", [1.0, 0, 0, 0, 0, 0, 0, 0])

    results = await store.search(
        tenant_id=tenant_id,
        scope_keys=frozenset({"workspace_public"}),
        class_="lore",
        query_embedding=[1.0, 0, 0, 0, 0, 0, 0, 0],
        k=10,
    )

    assert len(results) == 1


async def test_search_is_tenant_isolated(db_available: None) -> None:
    tenant_a, _, _ = await seed_dev_tenant(slug=f"vec-a-{uuid.uuid4().hex[:8]}")
    tenant_b, _, _ = await seed_dev_tenant(slug=f"vec-b-{uuid.uuid4().hex[:8]}")
    store: VectorStore = PgVectorStore()

    await _insert(tenant_a, "workspace_public", "lore", [1.0, 0, 0, 0, 0, 0, 0, 0])

    results = await store.search(
        tenant_id=tenant_b,
        scope_keys=frozenset({"workspace_public"}),
        class_="lore",
        query_embedding=[1.0, 0, 0, 0, 0, 0, 0, 0],
        k=10,
    )
    assert results == []
