"""``workspace_knowledge_attachment.priority_weight`` -> per-class retrieval weights.

The weight is authored per *attached source* ("this handbook matters more here than
there"), while the budget split it is meant to influence is per *class*. That gap is why
``apply_priority_weight_override`` and ``search_and_budget``'s ``priority_weights``
argument shipped fully built with nothing ever passing them: the translation between the
two was the missing piece, and without it a workspace could set a weight and observe no
effect anywhere.

**A source's own ``class_`` is the wrong thing to key on.** A repository ingests as one
source but its entries span all three classes -- `docs/adr/**` and `CONTRIBUTING*` become
`rules`, `docs/**` and `tasks/**` become `lore`, every source file becomes `misc`. Keying
on the source would credit that weight to exactly one of them. So the classes a source
contributes to are read from the chunks it actually published.

**Contribution is weighted by chunk count**, because a class's weight should reflect the
material really in it: a heavily-weighted source supplying most of a class moves it, and
one supplying a single chunk out of four hundred barely does. The result is a mean, so a
workspace where nobody has set anything gets 1.0 for every class and retrieval behaves
exactly as it did before this existed.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from core.tenancy.scope import tenant_scope

_NEUTRAL = 1.0


async def class_priority_weights(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> dict[str, float]:
    """Per-class multipliers for this workspace, or ``{}`` when nothing is weighted.

    Returning ``{}`` rather than a dict of 1.0s is deliberate: it lets a caller skip the
    override entirely, and keeps "no opinion" distinguishable from "deliberately neutral"
    in a trace.
    """
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT cnt.class AS class_, "
                    "       SUM(a.priority_weight * cnt.n) / NULLIF(SUM(cnt.n), 0) AS weight "
                    "FROM workspace_knowledge_attachment a "
                    "JOIN LATERAL ( "
                    "  SELECT e.class AS class, count(*) AS n "
                    "  FROM knowledge_entry e "
                    "  JOIN knowledge_chunk k ON k.entry_id = e.id "
                    "  JOIN knowledge_source s ON s.id = e.knowledge_source_id "
                    "  WHERE e.knowledge_source_id = a.knowledge_source_id "
                    "    AND e.version_id = COALESCE(a.version_pin, s.current_version_id) "
                    "  GROUP BY e.class "
                    ") cnt ON true "
                    "WHERE a.workspace_id = :workspace_id AND a.enabled "
                    "GROUP BY cnt.class"
                ),
                {"workspace_id": workspace_id},
            )
        ).all()

    weights = {str(class_): float(weight) for class_, weight in rows if weight is not None}
    # Every class neutral is the same as having no opinion; say so, and let the caller
    # skip a multiplication that cannot change anything.
    if all(abs(weight - _NEUTRAL) < 1e-9 for weight in weights.values()):
        return {}
    return weights
