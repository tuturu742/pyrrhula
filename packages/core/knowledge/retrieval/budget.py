"""Bucketed token budget: priority is a share of the
context budget, never a score multiplier. A rules bucket and a lore bucket never compete
for the same slot because they are never the same slot — this is what makes priority
deterministic and explainable ("rules got 75% of the budget because the phase says so"),
and it's why cross-class displacement is structurally impossible, not just unlikely.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from core.knowledge.retrieval.wrrf import FusedHit

DEFAULT_SPILL = "proportional"

# How much of a class's share the always-on (``constant``) entries may take before
# retrieval gets the rest.
#
# Without a ceiling, constants are simply first in line and can take all of it -- and did:
# on the karsh-vale sample every class of every phase was full of constants before a single
# search result was considered, so an attached rulebook of seven hundred chunks reached
# zero of thirty turns. A share rather than a count, because what matters is the room left
# behind, not how many entries an author wrote.
#
# The number is a default, not a law: ``fill_bucket`` takes it as a parameter so a flow
# could one day declare its own.
CONSTANT_BUCKET_SHARE = 0.6


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


def constant_allowance(
    token_counts: Sequence[int],
    token_budget: int,
    *,
    constant_share: float = CONSTANT_BUCKET_SHARE,
) -> tuple[int, int]:
    """How many always-on entries are *guaranteed* a place, and in how many tokens --
    ``fill_bucket``'s first pass, as arithmetic a report can run without a retrieval pass
    behind it.

    ``token_counts`` is in the order the constants will be offered (the author's
    ``insertion_order``). Two rules: the first one is admitted if it fits the bucket at
    all, because an always-on entry that never appears is not one; every one after it
    must fit within ``constant_share`` of the bucket, which is what leaves room for
    retrieval. Stop at the first that does not fit rather than skipping to a smaller
    one -- the author's order is a priority, and hopping over it would quietly reorder it.

    A floor, not a ceiling: ``fill_bucket`` offers the room retrieval does not spend back
    to the constants it held here, so more of them can land. What this function answers
    is the question a report can answer honestly in advance -- how many are certain.
    """
    cap = int(token_budget * constant_share)
    used = 0
    admitted = 0
    for index, tokens in enumerate(token_counts):
        limit = token_budget if index == 0 else cap
        if used + tokens > limit:
            break
        used += tokens
        admitted += 1
    return admitted, used


def fill_bucket(
    ranked: list[FusedHit],
    token_budget: int,
    *,
    constant_chunk_ids: frozenset[uuid.UUID] = frozenset(),
    constant_order: Mapping[uuid.UUID, int] | None = None,
    constant_share: float = CONSTANT_BUCKET_SHARE,
) -> BucketFillResult:
    """Three passes, in this order: always-on entries up to ``constant_share`` of the
    bucket, then retrieval in fused-rank order, then whatever room retrieval did not
    spend back to the always-on entries the share held back.

    Each pass stops — rather than skipping ahead to a smaller candidate — at the first
    hit that would exceed what is left: "until the bucket is exhausted", not best-effort
    bin-packing. A chunk is never partially included.

    The share is what fixed the failure this was written for: constants were simply first
    in line, and on a real sample they took every class of every phase, so an attached
    source of seven hundred chunks reached zero of thirty turns. The third pass is what
    keeps the share from overcorrecting -- an always-on entry should not be dropped to
    leave room that nothing then asks for.

    Constants are offered in the author's ``constant_order`` (``insertion_order`` on the
    entry), not in fused rank. It matters whenever they do not all fit: fused rank varies
    per turn, so "which always-on entries appeared" used to vary per turn too, which is
    the opposite of what an author declaring one is asking for. Ties fall back to fused
    rank and then chunk id, so the result is still total and deterministic.
    """
    order = constant_order or {}
    constants = sorted(
        (h for h in ranked if h.chunk_id in constant_chunk_ids),
        key=lambda h: (order.get(h.chunk_id, 0), h.rank, str(h.chunk_id)),
    )
    rest = [h for h in ranked if h.chunk_id not in constant_chunk_ids]

    included: list[FusedHit] = []
    leftover: list[FusedHit] = []
    used = 0

    admitted, _tokens = constant_allowance(
        [h.token_count for h in constants], token_budget, constant_share=constant_share
    )
    included.extend(constants[:admitted])
    used += sum(h.token_count for h in constants[:admitted])
    held_back = constants[admitted:]

    stopped = False
    for hit in rest:
        if stopped:
            leftover.append(hit)
            continue
        if used + hit.token_count > token_budget:
            stopped = True
            leftover.append(hit)
            continue
        included.append(hit)
        used += hit.token_count

    # Room retrieval declined goes back to the always-on entries the share held back.
    stopped = False
    for hit in held_back:
        if stopped or used + hit.token_count > token_budget:
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
    constant_order_by_class: dict[str, Mapping[uuid.UUID, int]] | None = None,
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
    order_by_class = constant_order_by_class or {}
    first_pass = {
        cls: fill_bucket(
            ranked_by_class.get(cls, []),
            budget,
            constant_chunk_ids=constant_by_class.get(cls, frozenset()),
            constant_order=order_by_class.get(cls),
        )
        for cls, budget in bucket_tokens.items()
    }

    if spill == "none":
        return first_pass

    # A bucket with something still waiting is never a donor, however many tokens it has
    # left over: since the share cap exists, "unused budget" and "candidates waiting" are
    # no longer mutually exclusive -- a bucket can hold room that only a candidate too
    # large for it could use.
    donors = {
        cls: bucket_tokens[cls] - result.tokens_used
        for cls, result in first_pass.items()
        if bucket_tokens[cls] - result.tokens_used > 0 and not result.leftover
    }
    recipients = {cls: result for cls, result in first_pass.items() if result.leftover}
    total_donated = sum(donors.values())
    if not recipients or total_donated <= 0:
        return first_pass

    total_recipient_weight = sum(bucket_tokens[cls] for cls in recipients)
    final: dict[str, BucketFillResult] = dict(first_pass)
    for cls, result in recipients.items():
        share = total_donated * (bucket_tokens[cls] / total_recipient_weight)
        # Spill is bonus budget, so the cap does not apply again: an always-on entry the
        # cap held back is exactly what the extra room is for, and it goes first.
        extra = fill_bucket(
            result.leftover,
            int(share),
            constant_chunk_ids=constant_by_class.get(cls, frozenset()),
            constant_order=order_by_class.get(cls),
            constant_share=1.0,
        )
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
