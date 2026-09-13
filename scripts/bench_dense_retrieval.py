"""A1.4: informational p95 latency baseline for dense retrieval on a synthetic
100k-chunk corpus. Not a CI gate (the task's acceptance criteria say so explicitly) --
run manually: ``uv run python scripts/bench_dense_retrieval.py``.

Bulk-inserts synthetic chunks directly via SQL (``generate_series`` + ``random()``), not
through the real ingestion/embedding pipeline -- this is measuring the retrieval query's
latency against a realistically-sized table, not exercising ingestion.
"""

from __future__ import annotations

import asyncio
import statistics
import time
import uuid

from sqlalchemy import text

from core.knowledge.authoring import create_source
from core.knowledge.retrieval.dense import search_dense
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_CORPUS_SIZE = 100_000
_QUERY_COUNT = 50


async def _bulk_seed(tenant_id: uuid.UUID, knowledge_source_id: uuid.UUID) -> None:
    async with tenant_scope(tenant_id) as session:
        entry_id = (
            await session.execute(
                text(
                    "INSERT INTO knowledge_entry "
                    "(tenant_id, knowledge_source_id, version_id, entry_key, title, "
                    " body_md, class, scope_key, keys, secondary_keys, logic, "
                    " use_regex, constant, position, insertion_order) "
                    "VALUES (:tenant_id, :source_id, NULL, 'bench', 'bench', "
                    " 'bench body', 'rules', 'workspace_public', '{}', '{}', 'AND', "
                    " false, false, 'before_char', 0) "
                    "RETURNING id"
                ),
                {"tenant_id": tenant_id, "source_id": knowledge_source_id},
            )
        ).scalar_one()

        await session.execute(
            text(
                "INSERT INTO knowledge_chunk "
                "(tenant_id, entry_id, version_id, ordinal, text, token_count, class, "
                " scope_key, embedding, content_hash) "
                "SELECT :tenant_id, :entry_id, NULL, gs, 'synthetic chunk ' || gs, 10, "
                "'rules', 'workspace_public', "
                "(SELECT ('[' || string_agg(round(random()::numeric, 4)::text, ',') "
                "|| ']')::vector FROM generate_series(1, 1024)), "
                "md5(gs::text) "
                "FROM generate_series(1, :n) AS gs"
            ),
            {"tenant_id": tenant_id, "entry_id": entry_id, "n": _CORPUS_SIZE},
        )


async def main() -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"bench-{uuid.uuid4().hex[:8]}"
    )
    source = await create_source(tenant_id, key="bench", name="Bench", class_="rules")

    print(f"seeding {_CORPUS_SIZE} synthetic chunks...")
    start = time.monotonic()
    await _bulk_seed(tenant_id, source.id)
    print(f"seeded in {time.monotonic() - start:.1f}s")

    query_embedding = [0.5] * 1024
    latencies_ms: list[float] = []
    for _ in range(_QUERY_COUNT):
        start = time.monotonic()
        await search_dense(
            tenant_id=tenant_id,
            scope_keys=frozenset({"workspace_public"}),
            class_="rules",
            query_embedding=query_embedding,
        )
        latencies_ms.append((time.monotonic() - start) * 1000)

    latencies_ms.sort()
    p50 = statistics.median(latencies_ms)
    p95 = latencies_ms[int(len(latencies_ms) * 0.95)]
    print(f"corpus={_CORPUS_SIZE} queries={_QUERY_COUNT} p50={p50:.1f}ms p95={p95:.1f}ms")


if __name__ == "__main__":
    asyncio.run(main())
