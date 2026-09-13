"""VisibilityResolver (C1.1, plan §7.2, §11.4 requirement 13): the single component that
answers "which scope keys may this principal read in this phase?" -- the mandatory,
pushed-down filter (INV-4) for every retrieval call, and the one visibility
implementation export (§11.4) and reporting (§11.5) must reuse rather than reimplement.

**Role resolution.** Human/service principals hold a workspace role via
``WorkspaceMembership`` (facilitator|participant|overseer|viewer); agents hold one via
their own ``Persona.persona_type`` (facilitator|participant|informational) -- "agents resolve
through their principal" means looking up the ``Persona`` row by ``principal_id``, not
expecting a ``WorkspaceMembership`` row to exist for them (it never does). A principal
with neither -- no relationship to this workspace at all -- resolves to no role and gets
an empty scope set unconditionally, including no private compartment (see below).

**Private compartments.** ``agent_private:<principal_id>`` is a *convention*, not a
``scope`` table row: any principal who has a role in the workspace is always granted their
own compartment, in every phase (including EXPORT), independent of whether the phase's
declared ``visibility.scopes`` mentions it -- process authors can't enumerate per-principal
keys they don't know at authoring time, and it can never leak (a principal can only ever
compute *their own* id's key). Pure groundwork for Phase 2's secret system today; nothing
reads or writes through it yet.

**EXPORT pseudo-phase.** ``scopes_for(..., visibility=EXPORT, session_id=None)`` skips the
phase-declared-list intersection entirely and returns every scope in the workspace the
principal is entitled to, full stop -- the same resolution logic, just an unfiltered
candidate set instead of one narrowed by a process definition's ``visibility.scopes``.
This is what makes reusing this function (instead of a parallel "export visibility"
implementation) both correct and easy for the future ExportService/ReportService.
"""

from __future__ import annotations

import uuid
from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.agents.models import Persona
from core.assembler.models import ScopeRow
from core.ports.scope import ScopeSet
from core.process.dsl.schema import VisibilitySpec
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.roles import role_satisfies
from core.tenancy.scope import tenant_scope

EXPORT: Final = "EXPORT"

# The two default scopes every workspace gets (task C1.1): a workspace-wide public
# compartment, and a facilitator-only one. Both reuse vocabulary already shared with
# WorkspaceMembership.role / Persona.persona_type -- 'facilitator' means the same thing in
# both places, deliberately.
_DEFAULT_SCOPES: Final[tuple[tuple[str, str, dict[str, object]], ...]] = (
    ("workspace_public", "public", {}),
    # Granted to the human facilitator (WorkspaceMembership.role) and the supervisor persona
    # (Persona.persona_type) alike -- both are "the facilitator" for visibility purposes.
    ("facilitator_only", "role", {"roles": ["facilitator", "supervisor"]}),
)


def agent_private_key(principal_id: uuid.UUID) -> str:
    return f"agent_private:{principal_id}"


async def seed_default_scopes(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    """Idempotent, matching the ``seed_dev_*`` convention elsewhere in this codebase."""
    async with tenant_scope(tenant_id) as session:
        existing_keys = set(
            (
                await session.execute(
                    select(ScopeRow.key).where(ScopeRow.workspace_id == workspace_id)
                )
            ).scalars()
        )
        for key, kind, members in _DEFAULT_SCOPES:
            if key in existing_keys:
                continue
            session.add(
                ScopeRow(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    key=key,
                    kind=kind,
                    members=members,
                )
            )


async def _resolve_role(
    session: AsyncSession, workspace_id: uuid.UUID, principal_id: uuid.UUID
) -> str | None:
    """``None`` means "no relationship to this workspace at all" -- distinct from holding
    a role that just doesn't happen to match any scope's declared roles."""
    principal = await session.get(Principal, principal_id)
    assert principal is not None

    if principal.kind == "agent":
        agent = await session.scalar(
            select(Persona).where(
                Persona.principal_id == principal_id, Persona.workspace_id == workspace_id
            )
        )
        return agent.persona_type if agent is not None else None

    membership = await session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.principal_id == principal_id,
        )
    )
    return membership.role if membership is not None else None


async def scopes_for(
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    workspace_id: uuid.UUID,
    visibility: VisibilitySpec | Literal["EXPORT"],
    session_id: uuid.UUID | None,
) -> ScopeSet:
    """``session_id`` is accepted (not defaulted away) as a forward seam for a future
    session-scoped compartment (Phase 2: who has been *told* something *in this
    session*) -- unused today. EXPORT always passes ``None``; there is no session."""
    del session_id  # unused today -- see docstring

    async with tenant_scope(tenant_id) as session:
        role = await _resolve_role(session, workspace_id, principal_id)
        if role is None:
            return ScopeSet()

        rows: list[ScopeRow]
        if visibility == EXPORT:
            rows = list(
                (
                    await session.execute(
                        select(ScopeRow).where(ScopeRow.workspace_id == workspace_id)
                    )
                ).scalars()
            )
        else:
            declared = set(visibility.scopes)
            rows = (
                list(
                    (
                        await session.execute(
                            select(ScopeRow).where(
                                ScopeRow.workspace_id == workspace_id, ScopeRow.key.in_(declared)
                            )
                        )
                    ).scalars()
                )
                if declared
                else []
            )

        entitled: set[str] = {agent_private_key(principal_id)}
        for row in rows:
            if _grants(row, role, principal_id):
                entitled.add(row.key)

    return ScopeSet(entitled)


def _grants(row: ScopeRow, role: str, principal_id: uuid.UUID) -> bool:
    if row.kind == "public":
        return True
    if row.kind == "role":
        roles = row.members.get("roles", [])
        # role_satisfies, not ``in``: a steward matches a facilitator-only or overseer-only
        # scope because a steward is both (core.tenancy.roles). The scope rows themselves
        # never mention steward, and older workspaces' rows predate it -- implication is
        # applied at match time so every already-seeded scope just works.
        return isinstance(roles, list) and any(role_satisfies(role, r) for r in roles)
    if row.kind in ("group", "private"):
        principal_ids = row.members.get("principal_ids", [])
        return isinstance(principal_ids, list) and str(principal_id) in principal_ids
    return False
