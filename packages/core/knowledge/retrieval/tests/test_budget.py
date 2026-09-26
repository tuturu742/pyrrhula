"""pure unit tests (no DB) for budget split and bucket fill."""

from __future__ import annotations

import uuid

from core.knowledge.retrieval.budget import (
    apply_priority_weight_override,
    fill_all_buckets,
    fill_bucket,
    split_budget,
    to_budgeted_chunks,
)
from core.knowledge.retrieval.wrrf import FusedHit


def _hit(token_count: int, rank: int, *, contributing: tuple[str, ...] = ("dense",)) -> FusedHit:
    return FusedHit(
        chunk_id=uuid.uuid4(),
        entry_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        version_id=None,
        entry_key="entry",
        token_count=token_count,
        rank=rank,
        wrrf_score=1.0 / rank,
        contributing_lists=contributing,
    )


# ── split_budget ────────────────────────────────────────────────────────────────────


def test_split_budget_matches_declared_ratio() -> None:
    buckets = split_budget({"rules": 0.75, "lore": 0.25}, 1000)
    assert buckets == {"rules": 750, "lore": 250}


def test_split_budget_normalises_ratios_not_summing_to_one() -> None:
    buckets = split_budget({"rules": 3, "lore": 1}, 1000)
    assert buckets == {"rules": 750, "lore": 250}


def test_split_budget_zero_total_gives_everyone_nothing() -> None:
    assert split_budget({"rules": 0, "lore": 0}, 1000) == {"rules": 0, "lore": 0}


def test_apply_priority_weight_override_scales_ratio() -> None:
    scaled = apply_priority_weight_override({"rules": 0.5}, {"rules": 2.0})
    assert scaled == {"rules": 1.0}


def test_apply_priority_weight_override_defaults_to_one() -> None:
    scaled = apply_priority_weight_override({"rules": 0.5, "lore": 0.5}, {"rules": 2.0})
    assert scaled == {"rules": 1.0, "lore": 0.5}


# ── fill_bucket ─────────────────────────────────────────────────────────────────────


def test_fill_bucket_includes_in_rank_order_until_budget_exhausted() -> None:
    hits = [_hit(40, 1), _hit(40, 2), _hit(40, 3)]
    result = fill_bucket(hits, token_budget=100)
    assert [h.rank for h in result.included] == [1, 2]
    assert result.tokens_used == 80


def test_fill_bucket_never_splits_a_chunk_mid_budget() -> None:
    hits = [_hit(60, 1), _hit(60, 2)]
    result = fill_bucket(hits, token_budget=100)
    # The second hit (60 tokens) would exceed the 40 remaining -- excluded entirely, not
    # partially.
    assert len(result.included) == 1
    assert result.tokens_used == 60


def test_fill_bucket_stops_at_first_overflow_even_if_a_later_hit_would_fit() -> None:
    hits = [_hit(60, 1), _hit(50, 2), _hit(10, 3)]
    result = fill_bucket(hits, token_budget=100)
    # Rank-order greedy: stops at hit 2 (60+50 > 100), never tries hit 3 even though
    # 60+10 <= 100 -- "until exhausted", not best-effort bin-packing.
    assert [h.rank for h in result.included] == [1]
    assert [h.rank for h in result.leftover] == [2, 3]


def test_fill_bucket_puts_constant_entries_first_regardless_of_rank() -> None:
    low_priority_constant = _hit(10, 5)
    high_ranked = _hit(10, 1)
    result = fill_bucket(
        [high_ranked, low_priority_constant],
        token_budget=100,
        constant_chunk_ids=frozenset({low_priority_constant.chunk_id}),
    )
    assert result.included[0].chunk_id == low_priority_constant.chunk_id


# ── fill_all_buckets / spill ─────────────────────────────────────────────────────────


def test_spill_none_leaves_unused_budget_unused() -> None:
    ranked = {"rules": [_hit(10, 1)], "lore": [_hit(10, 1), _hit(10, 2)]}
    result = fill_all_buckets(ranked, {"rules": 100, "lore": 10}, spill="none")
    assert result["rules"].tokens_used == 10  # 90 tokens of budget simply unused
    assert result["lore"].tokens_used == 10
    assert len(result["lore"].leftover) == 1  # the second lore hit didn't fit and stayed out


def test_spill_proportional_gives_unused_budget_to_a_bucket_that_needed_more() -> None:
    ranked = {"rules": [_hit(10, 1)], "lore": [_hit(10, 1), _hit(10, 2)]}
    # rules only has one 10-token candidate -- 90 tokens go unused there. lore has a
    # second candidate that didn't fit its own 10-token budget; spill should let it in.
    result = fill_all_buckets(ranked, {"rules": 100, "lore": 10}, spill="proportional")
    assert len(result["lore"].included) == 2
    assert result["lore"].tokens_used == 20


def test_spill_does_not_touch_a_bucket_that_used_its_full_budget_with_no_leftover() -> None:
    ranked = {"rules": [_hit(50, 1), _hit(50, 2)], "lore": [_hit(5, 1)]}
    result = fill_all_buckets(ranked, {"rules": 100, "lore": 100}, spill="proportional")
    # rules used its budget exactly with no excluded candidates -- nothing to spill INTO
    # it, and it's not a donor either.
    assert result["rules"].tokens_used == 100
    assert result["lore"].tokens_used == 5


# ── to_budgeted_chunks ────────────────────────────────────────────────────────────────


def test_to_budgeted_chunks_tags_class_and_why() -> None:
    constant_hit = _hit(10, 1, contributing=("keyed",))
    ranked = {"rules": [constant_hit]}
    fill_results = fill_all_buckets(ranked, {"rules": 100}, spill="none")
    chunks = to_budgeted_chunks(
        fill_results, constant_chunk_ids_by_class={"rules": frozenset({constant_hit.chunk_id})}
    )
    assert len(chunks) == 1
    assert chunks[0].class_ == "rules"
    assert chunks[0].bucket == "rules"
    assert chunks[0].why == "constant"


def test_to_budgeted_chunks_why_reflects_contributing_lists_when_not_constant() -> None:
    hit = _hit(10, 1, contributing=("dense", "sparse"))
    fill_results = fill_all_buckets({"rules": [hit]}, {"rules": 100}, spill="none")
    chunks = to_budgeted_chunks(fill_results)
    assert chunks[0].why == "dense+sparse"
