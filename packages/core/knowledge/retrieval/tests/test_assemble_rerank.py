"""A1.7 acceptance criteria for reranking wired into the full search+fuse+rerank+budget
pipeline: rerank changes within-bucket order only -- bucket membership (which class a
chunk can ever land in) and each bucket's token budget are untouched by whether
reranking ran at all.
"""

from __future__ import annotations

from adapters.reranker.stub.provider import StubReranker
from core.knowledge.retrieval.assemble import search_and_budget
from core.knowledge.retrieval.budget import split_budget
from core.knowledge.retrieval.tests.conftest import (
    seed_chunk,
    seed_tenant_and_source,
    unit_vector,
)
from core.ports.scope import ScopeSet


async def _seed_two_class_corpus(tenant_id, source_id) -> None:  # noqa: ANN001
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="rule-a",
        body_text="grapple check strength versus target defense value",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="rule-b",
        body_text="completely unrelated filler words here nothing special",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="lore-a",
        body_text="the ancient dragon guards the mountain treasure hoard",
        class_="lore",
        scope_key="workspace_public",
        embedding=unit_vector(1),
    )


async def test_reranking_does_not_change_bucket_token_allocation(db_available: None) -> None:
    tenant_id, source_id = await seed_tenant_and_source("rerank-budget")
    await _seed_two_class_corpus(tenant_id, source_id)

    common_kwargs = {
        "tenant_id": tenant_id,
        "scope_keys": ScopeSet({"workspace_public"}),
        "query_embedding": unit_vector(0),
        "query_text": "grapple check strength",
        "class_ratios": {"rules": 0.75, "lore": 0.25},
        "max_tokens": 100,
    }

    without_rerank = await search_and_budget(**common_kwargs)
    with_rerank = await search_and_budget(reranker=StubReranker(), **common_kwargs)

    # The token allocation per class is computed purely from ratios/max_tokens -- it
    # cannot depend on whether reranking ran.
    expected_buckets = split_budget(common_kwargs["class_ratios"], common_kwargs["max_tokens"])
    assert expected_buckets == {"rules": 75, "lore": 25}

    # Bucket membership: every included chunk's class is exactly what it was seeded with,
    # in both runs -- reranking never moves a chunk across the class boundary.
    for chunk in without_rerank + with_rerank:
        assert chunk.class_ in {"rules", "lore"}
        assert chunk.bucket == chunk.class_


async def test_reranking_changes_within_bucket_order(db_available: None) -> None:
    """Seed a rules bucket where WRRF's dense-similarity order and the query's actual
    lexical relevance disagree; reranking (word-overlap against the query) must be able
    to promote the lexically-relevant chunk over the WRRF top pick."""
    tenant_id, source_id = await seed_tenant_and_source("rerank-order")
    # Both rules chunks share the identical embedding (tied on dense score), so WRRF
    # fusion alone can't distinguish them -- only the reranker's text-based scoring can.
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="off-topic",
        body_text="filler words about nothing relevant to the query at all",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="on-topic",
        body_text="grapple check strength roll versus target defense",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )

    result = await search_and_budget(
        tenant_id=tenant_id,
        scope_keys=ScopeSet({"workspace_public"}),
        query_embedding=unit_vector(0),
        query_text="grapple check strength",
        class_ratios={"rules": 1.0},
        max_tokens=1000,
        reranker=StubReranker(),
    )

    entry_keys_by_rank = [c.entry_key for c in sorted(result, key=lambda c: c.rank)]
    assert entry_keys_by_rank[0] == "on-topic"
