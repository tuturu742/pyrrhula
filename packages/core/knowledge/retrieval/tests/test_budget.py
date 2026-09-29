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


# ── the constant share cap ──────────────────────────────────────────────────────────


def _keyed(token_count: int, rank: int, *, key: str = "entry") -> FusedHit:
    hit = _hit(token_count, rank, contributing=("keyed",))
    return FusedHit(**{**hit.__dict__, "entry_key": key})


def test_constants_stop_at_their_share_so_retrieval_keeps_room() -> None:
    """The measured failure this cap exists for: always-on entries filled every class of
    every phase of a shipped sample, and an attached source of seven hundred chunks
    reached zero of thirty turns."""
    constants = [_keyed(300, rank=1), _keyed(300, rank=2), _keyed(300, rank=3)]
    retrieved = [_hit(200, rank=4), _hit(200, rank=5)]
    ids = frozenset(h.chunk_id for h in constants)
    order = {h.chunk_id: i for i, h in enumerate(constants)}

    result = fill_bucket(constants + retrieved, 1000, constant_chunk_ids=ids, constant_order=order)

    # 60% of 1000 = 600: two constants, not three, and the remaining 400 is retrieval's,
    # which spends all of it -- so the third always-on entry stays out.
    included = [h.chunk_id for h in result.included]
    assert included[:2] == [constants[0].chunk_id, constants[1].chunk_id]
    assert constants[2].chunk_id not in included
    assert {h.chunk_id for h in retrieved} <= set(included)
    assert result.tokens_used == 1000
    assert constants[2].chunk_id in {h.chunk_id for h in result.leftover}


def test_room_retrieval_declines_goes_back_to_the_held_back_constants() -> None:
    """The share must not overcorrect: an always-on entry is not dropped to leave room
    that nothing else then asks for."""
    constants = [_keyed(300, rank=1), _keyed(300, rank=2), _keyed(300, rank=3)]
    ids = frozenset(h.chunk_id for h in constants)
    order = {h.chunk_id: i for i, h in enumerate(constants)}

    result = fill_bucket(constants, 1000, constant_chunk_ids=ids, constant_order=order)

    assert {h.chunk_id for h in result.included} == ids
    assert result.tokens_used == 900
    assert result.leftover == []


def test_the_first_constant_is_admitted_even_above_the_share() -> None:
    """An always-on entry that never appears is not one. One that fits the bucket at all
    gets in, and the class is then simply full -- which the saturation report says."""
    big = _keyed(900, rank=1)
    small = _hit(50, rank=2)
    result = fill_bucket([big, small], 1000, constant_chunk_ids=frozenset({big.chunk_id}))
    assert [h.chunk_id for h in result.included] == [big.chunk_id, small.chunk_id]
    assert result.tokens_used == 950


def test_a_constant_larger_than_the_whole_bucket_is_not_partially_included() -> None:
    big = _keyed(1200, rank=1)
    small = _hit(100, rank=2)
    result = fill_bucket([big, small], 1000, constant_chunk_ids=frozenset({big.chunk_id}))
    assert [h.chunk_id for h in result.included] == [small.chunk_id]


def test_constants_are_offered_in_the_authors_order_not_this_turns_ranking() -> None:
    """Which always-on entries survive a bucket too small for all of them used to depend
    on fused rank, which varies per turn -- so the set varied per turn. It is the
    author's `insertion_order` that decides."""
    first = _keyed(300, rank=9, key="first")  # last by rank, first by the author
    second = _keyed(300, rank=1, key="second")
    third = _keyed(300, rank=2, key="third")
    ids = frozenset({first.chunk_id, second.chunk_id, third.chunk_id})
    order = {first.chunk_id: 0, second.chunk_id: 1, third.chunk_id: 2}
    # A retrieval candidate that wants the whole remainder, so only the share's worth of
    # constants lands and the question "which two" is the one under test.
    filler = _hit(400, rank=3)

    result = fill_bucket(
        [second, third, first, filler], 1000, constant_chunk_ids=ids, constant_order=order
    )

    assert [h.entry_key for h in result.included if h.chunk_id in ids] == ["first", "second"]


def test_constant_allowance_is_the_floor_the_fill_guarantees() -> None:
    """The saturation report runs this arithmetic without a retrieval pass behind it, so
    the two must agree about which entries are certain of a place."""
    from core.knowledge.retrieval.budget import constant_allowance

    for budget, sizes in (
        (1000, [300, 300, 300]),
        (1000, [900, 50]),
        (500, [600]),
        (100, [10] * 20),
    ):
        constants = [_keyed(n, rank=i + 1) for i, n in enumerate(sizes)]
        ids = frozenset(h.chunk_id for h in constants)
        order = {h.chunk_id: i for i, h in enumerate(constants)}
        admitted, tokens = constant_allowance(sizes, budget)

        # With a retrieval candidate that takes exactly what the share left, the fill
        # lands exactly on the floor -- nothing remains for the third pass to give back.
        filler = [_hit(budget - tokens, rank=99)] if budget - tokens > 0 else []
        exact = fill_bucket(
            [*constants, *filler], budget, constant_chunk_ids=ids, constant_order=order
        )
        landed = [h for h in exact.included if h.chunk_id in ids]
        assert landed == constants[:admitted], (budget, sizes)
        assert sum(h.token_count for h in landed) == tokens, (budget, sizes)

        # With nothing competing, the fill may exceed the floor but never fall below it.
        alone = fill_bucket(constants, budget, constant_chunk_ids=ids, constant_order=order)
        assert len(alone.included) >= admitted, (budget, sizes)


def test_spill_offers_a_held_back_constant_the_extra_room_first() -> None:
    """Spill is bonus budget, and an always-on entry the cap held back is exactly what it
    is for -- so the cap does not apply again there."""
    constants = [_keyed(300, rank=1), _keyed(300, rank=2), _keyed(300, rank=3)]
    ids = frozenset(h.chunk_id for h in constants)
    order = {h.chunk_id: i for i, h in enumerate(constants)}
    # 700 holds two of the three (600) and cannot hold the third in the 100 that remain.
    results = fill_all_buckets(
        {"rules": constants, "lore": [_hit(10, rank=1)]},
        {"rules": 700, "lore": 1000},
        constant_chunk_ids_by_class={"rules": ids},
        constant_order_by_class={"rules": order},
    )
    # lore used 10 of 1000 and has nothing waiting, so it donates; rules has something
    # waiting, so it receives -- and the third always-on entry lands.
    assert constants[2].chunk_id in {h.chunk_id for h in results["rules"].included}
