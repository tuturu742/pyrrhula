"""Designated-overseer requirement: a workspace with multiple
unrelated humans, or any enterprise-tenant workspace, must have a principal holding the
`overseer` workspace role before it can be configured -- an agent-only or solo-human
workspace does not. "Enterprise-tenant" is read from the workspace's own resolved
vocabulary overlay (`enterprise_v1`), not a separate ad-hoc tenant classification field
-- the overlay is already this codebase's one mechanism for "which domain is this",
so reusing it here avoids a second, competing way to ask the same question.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.moderation.policy import get_policy
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.roles import roles_satisfying
from core.tenancy.scope import tenant_scope
from core.vocabulary.service import resolve_overlay_for_workspace

_ENTERPRISE_OVERLAY_KEY = "enterprise_v1"
_MULTI_HUMAN_THRESHOLD = 2


class OverseerRequiredError(Exception):
    pass


async def validate_overseer_requirement(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    async with tenant_scope(tenant_id) as session:
        human_principal_ids = (
            await session.execute(
                select(WorkspaceMembership.principal_id)
                .join(Principal, Principal.id == WorkspaceMembership.principal_id)
                .where(WorkspaceMembership.workspace_id == workspace_id, Principal.kind == "human")
            )
        ).scalars()
        distinct_humans = set(human_principal_ids)

        # A steward is an overseer too (core.tenancy.roles): a solo workspace whose
        # creator holds the combined seat satisfies Q6's "there is an overseer" without a
        # second human. roles_satisfying pushes that down as the predicate.
        has_overseer = await session.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.role.in_(roles_satisfying("overseer")),
            )
        )

    overlay = await resolve_overlay_for_workspace(tenant_id, workspace_id)
    is_enterprise = overlay is not None and overlay.key == _ENTERPRISE_OVERLAY_KEY
    is_multi_human = len(distinct_humans) >= _MULTI_HUMAN_THRESHOLD

    if (is_multi_human or is_enterprise) and has_overseer is None:
        # The requirement's *second arm*: "an overseer **or** moderation with
        # overseer-equivalent visibility". The first arm came first and left this one
        # open until there was a moderation layer to point at. `enabled` alone is
        # not enough -- scanning that nobody reads is not oversight, so the tenant must
        # also have said their moderation surfaces flagged content to a human who can act.
        policy = await get_policy(tenant_id)
        if policy.enabled and policy.overseer_equivalent:
            return

        reason = "multiple human participants" if is_multi_human else "enterprise tenant"
        raise OverseerRequiredError(
            f"workspace {workspace_id} requires a designated overseer ({reason}), or "
            "tenant moderation with overseer-equivalent visibility enabled"
        )
