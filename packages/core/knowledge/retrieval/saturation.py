"""Is there room to retrieve anything? -- the arithmetic an author cannot see.

A phase's knowledge budget is split into per-class buckets, and each bucket is filled with
``constant`` entries first (the author's always-on text), up to a share of it, and then
with whatever retrieval found. Two things can go wrong and neither is visible from either
end: the always-on entries can take all the room a share allows and leave retrieval with
too little to place a chunk, and there can be more of them than fit at all, in which case
some are simply not there.

Both were live. On the karsh-vale sample every class of every phase was over its share --
rules 1,936 tokens against shares of 360-1,470, lore 1,821 against 630-1,155 -- so an
attached rulebook of seven hundred chunks reached zero of thirty measured turns, and the
sample's own lorebook was never retrieved either: every lore entry that appeared was an
always-on one that happened to fit.

The failure is silent by construction -- a bucket that fills is exactly what a bucket is
for -- so it shows up only as an absence, which reads as "the model ignored the handbook".
This module computes the numbers so the product can say it out loud instead.

Two deliberate choices:

**Phase scopes, not a viewer's.** Constants are counted over every scope the phase
declares, which is the load for the most-entitled persona. A persona entitled to fewer
scopes sees less. "Saturated" here therefore means "saturated for someone", which is the
answer an author wants; the alternative would be a different verdict per cast member.

**Chunk tokens, not entry lengths.** The bucket is filled with chunks and counts their
``token_count``, so that is what is summed here -- an entry whose body was never chunked
occupies nothing, and this module reports that faithfully rather than guessing from its
prose.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text

from core.knowledge.retrieval.budget import (
    apply_priority_weight_override,
    constant_allowance,
    split_budget,
)
from core.knowledge.retrieval.priority import class_priority_weights
from core.knowledge.retrieval.versions import effective_version_ids
from core.tenancy.scope import tenant_scope

# One average chunk, by the ingestion chunker's target size: below this a bucket has
# room on paper and not in practice.
TIGHT_THRESHOLD_TOKENS = 400


@dataclass(frozen=True)
class ClassSaturation:
    """One class's arithmetic in one phase."""

    class_: str
    # What the phase's budget gives this class, after the history reservation and the
    # workspace's priority weights -- the same number `search_and_budget` will use.
    bucket_tokens: int
    # What this class's always-on entries would occupy if they all got in.
    constant_tokens: int
    constant_entries: int
    # What the share cap actually admits of them, by `budget.constant_allowance`.
    admitted_constant_tokens: int
    admitted_constant_entries: int
    # Every always-on entry of this class in this phase, in the order the budget offers
    # them (the author's `insertion_order`). The ones past `admitted_constant_entries`
    # are the ones that do not fit.
    constant_entry_keys: list[str]

    @property
    def dropped_constant_entries(self) -> int:
        """Always-on entries that do not fit the share and so are not in any turn."""
        return self.constant_entries - self.admitted_constant_entries

    @property
    def retrievable_tokens(self) -> int:
        return max(self.bucket_tokens - self.admitted_constant_tokens, 0)

    @property
    def saturated(self) -> bool:
        """No room for even one retrieved chunk. Retrieval still runs; nothing survives.
        With the share cap this needs a single always-on entry large enough to fill the
        whole bucket -- the cap admits the first one whatever its size, because an
        always-on entry that never appears is not one."""
        return self.retrievable_tokens <= 0

    @property
    def tight(self) -> bool:
        """Room, but less than one average chunk's worth. Technically retrieving,
        practically one small chunk on a good day."""
        return not self.saturated and self.retrievable_tokens < TIGHT_THRESHOLD_TOKENS


@dataclass(frozen=True)
class PhaseSaturation:
    phase_key: str
    max_tokens: int
    history_reserved_tokens: int
    classes: list[ClassSaturation]

    @property
    def saturated_classes(self) -> list[ClassSaturation]:
        return [c for c in self.classes if c.saturated]


_CONSTANT_LOAD_SQL = (
    "SELECT e.class AS class_, e.scope_key AS scope_key, e.entry_key AS entry_key, "
    "       e.insertion_order AS insertion_order, "
    "       COALESCE(SUM(c.token_count), 0) AS tokens "
    "FROM knowledge_entry e "
    "JOIN knowledge_chunk c ON c.entry_id = e.id "
    "WHERE e.tenant_id = :tenant_id "
    "  AND e.constant "
    "  AND e.version_id = ANY(:version_ids) "
    "  AND NOT c.quarantined "
    "GROUP BY e.class, e.scope_key, e.entry_key, e.insertion_order"
)


@dataclass(frozen=True)
class _ConstantEntry:
    class_: str
    scope_key: str
    entry_key: str
    insertion_order: int
    tokens: int


async def _constant_load(
    tenant_id: uuid.UUID, version_ids: frozenset[uuid.UUID]
) -> list[_ConstantEntry]:
    """Every always-on entry this workspace reads, with the tokens its chunks occupy."""
    if not version_ids:
        return []
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                text(_CONSTANT_LOAD_SQL),
                {"tenant_id": tenant_id, "version_ids": list(version_ids)},
            )
        ).all()
    return [
        _ConstantEntry(
            class_=row[0],
            scope_key=row[1],
            entry_key=row[2],
            insertion_order=int(row[3]),
            tokens=int(row[4]),
        )
        for row in rows
    ]


async def workspace_saturation(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, dsl: object
) -> list[PhaseSaturation]:
    """Per phase of ``dsl``, per class it budgets: bucket size against constant load.

    ``dsl`` is a validated ``ProcessDefinitionDSL`` (taken as ``object`` so this module
    does not drag the process DSL into the retrieval package -- it reads two attributes
    and nothing else). Phases with no budget are skipped: a pure-await phase retrieves
    nothing, so there is nothing to be saturated.
    """
    version_ids = await effective_version_ids(tenant_id, workspace_id)
    weights = await class_priority_weights(tenant_id, workspace_id)
    constants = await _constant_load(tenant_id, version_ids)

    out: list[PhaseSaturation] = []
    phases: dict[str, object] = getattr(dsl, "phases", {}) or {}
    for phase_key, phase in phases.items():
        budget = getattr(phase, "budget", None)
        if budget is None:
            continue
        history_reserved = phase.history_slice_tokens()  # type: ignore[attr-defined]
        ratios = dict(budget.ratio)
        if weights:
            ratios = apply_priority_weight_override(ratios, weights)
        buckets = split_budget(ratios, budget.max_tokens - history_reserved)
        visibility = getattr(phase, "visibility", None)
        scopes = set(getattr(visibility, "scopes", []) or [])
        # The phase's own share when it names one, so the report and the fill agree.
        share = getattr(budget, "constant_share", None)

        classes: list[ClassSaturation] = []
        for class_, bucket_tokens in buckets.items():
            in_play = [c for c in constants if c.class_ == class_ and c.scope_key in scopes]
            # The order the budget will offer them in, so "these fit, those do not" here
            # names the same entries the fill will.
            in_play.sort(key=lambda c: (c.insertion_order, c.entry_key))
            admitted, admitted_tokens = constant_allowance(
                [c.tokens for c in in_play],
                bucket_tokens,
                **({} if share is None else {"constant_share": share}),
            )
            classes.append(
                ClassSaturation(
                    class_=class_,
                    bucket_tokens=bucket_tokens,
                    constant_tokens=sum(c.tokens for c in in_play),
                    constant_entries=len(in_play),
                    admitted_constant_tokens=admitted_tokens,
                    admitted_constant_entries=admitted,
                    constant_entry_keys=[c.entry_key for c in in_play],
                )
            )
        classes.sort(key=lambda c: c.class_)
        out.append(
            PhaseSaturation(
                phase_key=phase_key,
                max_tokens=budget.max_tokens,
                history_reserved_tokens=history_reserved,
                classes=classes,
            )
        )
    out.sort(key=lambda p: p.phase_key)
    return out
