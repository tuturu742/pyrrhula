"""pure unit tests (no DB) for WRRF fusion."""

from __future__ import annotations

import uuid

from core.knowledge.retrieval.dense import RetrievalHit
from core.knowledge.retrieval.wrrf import fuse


def _hit(chunk_id: uuid.UUID, rank: int, *, entry_id: uuid.UUID | None = None) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id,
        entry_id=entry_id or uuid.uuid4(),
        source_id=uuid.uuid4(),
        version_id=None,
        entry_key="entry",
        token_count=10,
        rank=rank,
        score=1.0 / rank,
    )


def test_top_ranked_in_a_single_list_wins() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    fused = fuse({"dense": [_hit(a, 1), _hit(b, 2)]})
    assert [f.chunk_id for f in fused] == [a, b]
    assert fused[0].rank == 1
    assert fused[0].contributing_lists == ("dense",)


def test_appearing_in_multiple_lists_outranks_a_single_list_top_hit() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    # `a` is rank 1 in both lists; `b` is rank 1 in dense only but absent from sparse --
    # `a`'s combined WRRF score must exceed a lone top rank.
    fused = fuse({"dense": [_hit(b, 1), _hit(a, 2)], "sparse": [_hit(a, 1)]})
    assert fused[0].chunk_id == a
    assert set(fused[0].contributing_lists) == {"dense", "sparse"}


def test_list_weights_change_the_outcome() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    candidates = {"dense": [_hit(a, 1), _hit(b, 2)], "sparse": [_hit(b, 1), _hit(a, 2)]}

    dense_favoured = fuse(candidates, list_weights={"dense": 10.0, "sparse": 0.1})
    assert dense_favoured[0].chunk_id == a

    sparse_favoured = fuse(candidates, list_weights={"dense": 0.1, "sparse": 10.0})
    assert sparse_favoured[0].chunk_id == b


def test_same_input_always_produces_same_output() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    candidates = {
        "dense": [_hit(a, 1), _hit(b, 2), _hit(c, 3)],
        "sparse": [_hit(c, 1), _hit(a, 2)],
    }
    first = fuse(candidates)
    second = fuse(candidates)
    assert [f.chunk_id for f in first] == [f.chunk_id for f in second]
    assert [f.wrrf_score for f in first] == [f.wrrf_score for f in second]


def test_ranks_are_dense_one_indexed_and_contiguous() -> None:
    hits = [_hit(uuid.uuid4(), i + 1) for i in range(5)]
    fused = fuse({"dense": hits})
    assert [f.rank for f in fused] == [1, 2, 3, 4, 5]


def test_empty_candidate_lists_produce_no_hits() -> None:
    assert fuse({"dense": [], "sparse": []}) == []
