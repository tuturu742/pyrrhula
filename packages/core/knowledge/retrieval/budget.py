"""Bucketed token budget: priority is a share of the
context budget, never a score multiplier. A rules bucket and a lore bucket never compete
for the same slot because they are never the same slot — this is what makes priority
deterministic and explainable ("rules got 75% of the budget because the phase says so"),
and it's why cross-class displacement is structurally impossible, not just unlikely.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from core.knowledge.retrieval.wrrf import FusedHit

DEFAULT_SPILL = "proportional"


def split_budget(ratios: dict[str, float], max_tokens: int) -> dict[str, int]:
    """Normalises ``ratios`` (they need not already sum to 1) and splits ``max_tokens``
    proportionally. A class absent from ``ratios`` gets no budget — silence is "not
    included this phase", not "included for free"."""
    total = sum(ratios.values())
    if total <= 0:
        return dict.fromkeys(ratios, 0)
    return {cls: int(max_tokens * (ratio / total)) for cls, ratio in ratios.items()}


def apply_priority_weight_override(
    base_ratios: dict[str, float], priority_weights: dict[str, float]
) -> dict[str, float]:
    """``workspace_knowledge_attachment.priority_weight`` scales a class's base
    ratio before the split — two workspaces sharing the same ``phase.budget.ratio`` can
    still end up with different effective splits if one attached its rules source at a
    higher ``priority_weight`` than the other."""
    return {cls: ratio * priority_weights.get(cls, 1.0) for cls, ratio in base_ratios.items()}


@dataclass(frozen=True)
class BucketFillResult:
    included: list[FusedHit]
    tokens_used: int
    leftover: list[FusedHit] = field(default_factory=list)


def fill_bucket(
    ranked: list[FusedHit],
    token_budget: int,
    *,
    constant_chunk_ids: frozenset[uuid.UUID] = frozenset(),
) -> BucketFillResult:
    """Constant entries first (in their own fused-rank order among themselves), then the
    rest in fused-rank order, stopping — not skipping ahead to a smaller one — at the
    first hit that would exceed the remaining budget: "until the bucket is exhausted",
    not best-effort bin-packing. A chunk is never partially included."""
    constant_first = [h for h in ranked if h.chunk_id in constant_chunk_ids]
    rest = [h for h in ranked if h.chunk_id not in constant_chunk_ids]
    ordered = constant_first + rest

    included: list[FusedHit] = []
    leftover: list[FusedHit] = []
    used = 0
    stopped = False
    for hit in ordered:
        if stopped:
            leftover.append(hit)
            continue
        if used + hit.token_count > token_budget:
            stopped = True
            leftover.append(hit)
            continue
        included.append(hit)
        used += hit.token_count

    return BucketFillResult(included=included, tokens_used=used, leftover=leftover)


def fill_all_buckets(
    ranked_by_class: dict[str, list[FusedHit]],
    bucket_tokens: dict[str, int],
    *,
    constant_chunk_ids_by_class: dict[str, frozenset[uuid.UUID]] | None = None,
    spill: str = DEFAULT_SPILL,
) -> dict[str, BucketFillResult]:
    """Fills each class's bucket independently, then — ``spill='proportional'`` (default)
    — redistributes any bucket's *unused* budget (it ran out of ranked candidates before
    exhausting its tokens) to buckets that had the opposite problem (ran out of budget
    with more ranked candidates still waiting), weighted by each recipient's own original
    bucket size. ``spill='none'`` leaves unused budget unused. One redistribution pass,
    not an iterative fixed point — a documented simplification, not a hidden one.
    """
    constant_by_class = constant_chunk_ids_by_class or {}
    first_pass = {
        cls: fill_bucket(
            ranked_by_class.get(cls, []),
            budget,
            constant_chunk_ids=constant_by_class.get(cls, frozenset()),
        )
        for cls, budget in bucket_tokens.items()
    }

    if spill == "none":
        return first_pass

    donors = {
        cls: bucket_tokens[cls] - result.tokens_used
        for cls, result in first_pass.items()
        if bucket_tokens[cls] - result.tokens_used > 0
    }
    recipients = {
        cls: result for cls, result in first_pass.items() if result.leftover and cls not in donors
    }
    total_donated = sum(donors.values())
    if not recipients or total_donated <= 0:
        return first_pass

    total_recipient_weight = sum(bucket_tokens[cls] for cls in recipients)
    final: dict[str, BucketFillResult] = dict(first_pass)
    for cls, result in recipients.items():
        share = total_donated * (bucket_tokens[cls] / total_recipient_weight)
        extra = fill_bucket(result.leftover, int(share))
        final[cls] = BucketFillResult(
            included=result.included + extra.included,
            tokens_used=result.tokens_used + extra.tokens_used,
            leftover=extra.leftover,
        )
    return final


@dataclass(frozen=True)
class BudgetedChunk:
    """The manifest's raw material: every included chunk carries
    enough to answer "why is this here" without a further query."""

    chunk_id: uuid.UUID
    entry_id: uuid.UUID
    entry_key: str
    source_id: uuid.UUID
    version_id: uuid.UUID | None
    class_: str
    bucket: str
    rank: int
    score: float
    why: str
    token_count: int


def to_budgeted_chunks(
    fill_results: dict[str, BucketFillResult],
    *,
    constant_chunk_ids_by_class: dict[str, frozenset[uuid.UUID]] | None = None,
) -> list[BudgetedChunk]:
    constant_by_class = constant_chunk_ids_by_class or {}
    chunks: list[BudgetedChunk] = []
    for class_, result in fill_results.items():
        constant_ids = constant_by_class.get(class_, frozenset())
        for hit in result.included:
            why = "constant" if hit.chunk_id in constant_ids else "+".join(hit.contributing_lists)
            chunks.append(
                BudgetedChunk(
                    chunk_id=hit.chunk_id,
                    entry_id=hit.entry_id,
                    entry_key=hit.entry_key,
                    source_id=hit.source_id,
                    version_id=hit.version_id,
                    class_=class_,
                    bucket=class_,
                    rank=hit.rank,
                    score=hit.wrrf_score,
                    why=why,
                    token_count=hit.token_count,
                )
            )
    return chunks
