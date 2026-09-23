"""A1.6 acceptance criteria for the full search+fuse+budget pipeline, against a live
Postgres. This is the golden test the Phase-1 exit gate references: "budget ratio change
-> visibly different retrieval."
"""

from __future__ import annotations

from core.knowledge.retrieval.assemble import search_and_budget
from core.knowledge.retrieval.tests.conftest import (
    seed_chunk,
    seed_tenant_and_source,
    unit_vector,
    versions_of,
)
from core.ports.scope import ScopeSet

_TOKENS_PER_CHUNK = 10  # each seeded body is exactly 10 words


def _body(n: int) -> str:
    return " ".join([f"word{n}"] * _TOKENS_PER_CHUNK)


async def _seed_fixed_corpus(tenant_id, source_id) -> None:  # noqa: ANN001
    # 4 rules chunks + 4 lore chunks, all matching the same query embedding/text, so
    # without bucketing they'd freely compete for slots on raw signal strength.
    for i in range(4):
        await seed_chunk(
            tenant_id,
            source_id,
            entry_key=f"rule-{i}",
            body_text=_body(i),
            class_="rules",
            scope_key="workspace_public",
            embedding=unit_vector(0),
        )
    for i in range(4):
        await seed_chunk(
            tenant_id,
            source_id,
            entry_key=f"lore-{i}",
            body_text=_body(i + 100),
            class_="lore",
            scope_key="workspace_public",
            embedding=unit_vector(0),
        )


async def test_budget_ratio_change_produces_different_included_sets(
    db_available: None,
) -> None:
    tenant_id, source_id = await seed_tenant_and_source("assemble-golden")
    await _seed_fixed_corpus(tenant_id, source_id)

    common_kwargs = {
        "tenant_id": tenant_id,
        "scope_keys": ScopeSet({"workspace_public"}),
        "query_embedding": unit_vector(0),
        "query_text": "word0",
        "max_tokens": 40,
        "version_set": await versions_of(tenant_id),
    }

    rules_heavy = await search_and_budget(
        class_ratios={"rules": 0.75, "lore": 0.25}, **common_kwargs
    )
    lore_heavy = await search_and_budget(
        class_ratios={"rules": 0.25, "lore": 0.75}, **common_kwargs
    )

    rules_heavy_by_class = {"rules": 0, "lore": 0}
    for chunk in rules_heavy:
        rules_heavy_by_class[chunk.class_] += 1
    lore_heavy_by_class = {"rules": 0, "lore": 0}
    for chunk in lore_heavy:
        lore_heavy_by_class[chunk.class_] += 1

    # 40 tokens * 0.75 = 30 tokens = 3 chunks @ 10 tokens each; 40 * 0.25 = 10 = 1 chunk.
    assert rules_heavy_by_class == {"rules": 3, "lore": 1}
    # Ratios flipped -> counts flip too, fully explained by the same bucket math.
    assert lore_heavy_by_class == {"rules": 1, "lore": 3}


async def test_rules_and_lore_chunks_never_compete_for_the_same_slot(
    db_available: None,
) -> None:
    """Cross-class displacement is impossible by construction: give lore an objectively
    stronger per-chunk signal (exact embedding + keyword match) than rules, but confirm
    rules still gets its full budget allocation regardless -- because they were never in
    the same competition to begin with."""
    tenant_id, source_id = await seed_tenant_and_source("assemble-isolation")
    # Rules chunks: weaker signal (orthogonal embedding, no keyword match).
    for i in range(2):
        await seed_chunk(
            tenant_id,
            source_id,
            entry_key=f"rule-{i}",
            body_text="unrelated filler content here nothing special at all",
            class_="rules",
            scope_key="workspace_public",
            embedding=unit_vector(5),
        )
    # Lore chunks: strong signal (exact embedding + exact keyword match).
    for i in range(2):
        await seed_chunk(
            tenant_id,
            source_id,
            entry_key=f"lore-{i}",
            body_text="the ancient dragon word0 word0 word0 word0 word0 word0",
            class_="lore",
            scope_key="workspace_public",
            embedding=unit_vector(0),
        )

    result = await search_and_budget(
        tenant_id=tenant_id,
        scope_keys=ScopeSet({"workspace_public"}),
        query_embedding=unit_vector(0),
        query_text="word0",
        class_ratios={"rules": 0.5, "lore": 0.5},
        max_tokens=40,
        version_set=await versions_of(tenant_id),
    )

    by_class = {"rules": 0, "lore": 0}
    for chunk in result:
        by_class[chunk.class_] += 1

    # Despite lore's objectively stronger match, rules still gets its full 50% share --
    # a lore chunk never displaced a rules slot.
    assert by_class["rules"] == 2
    assert by_class["lore"] == 2
