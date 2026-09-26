"""Acceptance criteria for the VisibilityResolver, against a live Postgres."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.agents.models import Persona
from core.agents.seed import seed_dev_agent
from core.assembler.models import ScopeRow
from core.assembler.visibility import EXPORT, agent_private_key, scopes_for, seed_default_scopes
from core.process.dsl.schema import VisibilitySpec
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


def _visibility(scopes: list[str]) -> VisibilitySpec:
    return VisibilitySpec(
        knowledge_classes=["rules"], scopes=scopes, entity_fields="all", secrets="none"
    )


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    """Returns (tenant_id, workspace_id, principal_ids) where principal_ids has keys
    'facilitator', 'participant', 'viewer' (human WorkspaceMembership roles) and 'agent'
    (an Persona with persona_type='supervisor', to prove role-vocabulary overlap works)."""
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )

    principal_ids: dict[str, uuid.UUID] = {}
    async with tenant_scope(tenant_id) as session:
        for role in ("facilitator", "participant", "viewer"):
            principal = Principal(tenant_id=tenant_id, kind="human", display_name=role)
            session.add(principal)
            await session.flush()
            session.add(
                WorkspaceMembership(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    principal_id=principal.id,
                    role=role,
                )
            )
            principal_ids[role] = principal.id

    persona_id = await seed_dev_agent(
        tenant_id, workspace_id, key=f"{slug_prefix}-agent", persona_type="supervisor"
    )
    async with tenant_scope(tenant_id) as session:
        agent = await session.get(Persona, persona_id)
        assert agent is not None
        principal_ids["agent"] = agent.principal_id

    return tenant_id, workspace_id, principal_ids


# ── matrix: (facilitator, participant, viewer, agent) x phases with differing scopes ──


async def test_scope_matrix_across_roles_and_phases(db_available: None) -> None:
    tenant_id, workspace_id, principals = await _setup("vis-matrix")

    public_only = _visibility(["workspace_public"])
    public_and_facilitator = _visibility(["workspace_public", "facilitator_only"])

    for role in ("facilitator", "participant", "viewer", "agent"):
        result = await scopes_for(
            tenant_id, principals[role], workspace_id, public_only, session_id=None
        )
        assert result == {"workspace_public", agent_private_key(principals[role])}

    for role in ("participant", "viewer"):
        result = await scopes_for(
            tenant_id, principals[role], workspace_id, public_and_facilitator, session_id=None
        )
        assert result == {"workspace_public", agent_private_key(principals[role])}

    # Both the human facilitator AND the agent with persona_type='supervisor' qualify for
    # the role-kind scope -- the vocabularies are deliberately shared.
    for role in ("facilitator", "agent"):
        result = await scopes_for(
            tenant_id, principals[role], workspace_id, public_and_facilitator, session_id=None
        )
        assert result == {
            "workspace_public",
            "facilitator_only",
            agent_private_key(principals[role]),
        }


async def test_phase_declaring_no_scopes_yields_only_the_private_compartment(
    db_available: None,
) -> None:
    tenant_id, workspace_id, principals = await _setup("vis-empty-phase")
    result = await scopes_for(
        tenant_id, principals["participant"], workspace_id, _visibility([]), session_id=None
    )
    assert result == {agent_private_key(principals["participant"])}


async def test_principal_with_no_workspace_relationship_gets_nothing(db_available: None) -> None:
    tenant_id, workspace_id, _principals = await _setup("vis-outsider")
    async with tenant_scope(tenant_id) as session:
        outsider = Principal(tenant_id=tenant_id, kind="human", display_name="outsider")
        session.add(outsider)
        await session.flush()
        outsider_id = outsider.id

    result = await scopes_for(
        tenant_id, outsider_id, workspace_id, _visibility(["workspace_public"]), session_id=None
    )
    assert result == set()  # not even their own agent_private key -- no relationship at all


# ── private/group scope member exclusion ────────────────────────────────────────────


async def test_principal_outside_private_scope_members_never_receives_its_key(
    db_available: None,
) -> None:
    tenant_id, workspace_id, principals = await _setup("vis-private")
    async with tenant_scope(tenant_id) as session:
        session.add(
            ScopeRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                key="inner_circle",
                kind="private",
                members={"principal_ids": [str(principals["facilitator"])]},
            )
        )

    visibility = _visibility(["workspace_public", "inner_circle"])

    member_result = await scopes_for(
        tenant_id, principals["facilitator"], workspace_id, visibility, session_id=None
    )
    assert "inner_circle" in member_result

    for role in ("participant", "viewer", "agent"):
        outsider_result = await scopes_for(
            tenant_id, principals[role], workspace_id, visibility, session_id=None
        )
        assert "inner_circle" not in outsider_result


async def test_no_principal_ever_receives_anothers_private_compartment_key(
    db_available: None,
) -> None:
    """The agent_private:<id> convention itself: declaring someone else's private key in
    a phase's visibility.scopes must never grant it to anyone but its owner (there is no
    `scope` row backing these keys at all -- this is the structural guarantee, not a
    members-list check)."""
    tenant_id, workspace_id, principals = await _setup("vis-agent-private")
    someone_elses_key = agent_private_key(principals["facilitator"])
    visibility = _visibility(["workspace_public", someone_elses_key])

    result = await scopes_for(
        tenant_id, principals["participant"], workspace_id, visibility, session_id=None
    )
    assert someone_elses_key not in result
    assert agent_private_key(principals["participant"]) in result


# ── EXPORT pseudo-phase ──────────────────────────────────────────────────────────────


async def test_export_returns_full_legitimate_scope_set_independent_of_any_phase(
    db_available: None,
) -> None:
    tenant_id, workspace_id, principals = await _setup("vis-export")
    async with tenant_scope(tenant_id) as session:
        session.add(
            ScopeRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                key="side_channel",
                kind="group",
                members={"principal_ids": [str(principals["facilitator"])]},
            )
        )

    facilitator_export = await scopes_for(
        tenant_id, principals["facilitator"], workspace_id, EXPORT, session_id=None
    )
    assert facilitator_export == {
        "workspace_public",
        "facilitator_only",
        "side_channel",
        agent_private_key(principals["facilitator"]),
    }

    participant_export = await scopes_for(
        tenant_id, principals["participant"], workspace_id, EXPORT, session_id=None
    )
    assert participant_export == {
        "workspace_public",
        agent_private_key(principals["participant"]),
    }
    assert "facilitator_only" not in participant_export
    assert "side_channel" not in participant_export


async def test_export_matches_phase_scoped_result_when_phase_declares_everything(
    db_available: None,
) -> None:
    """Golden test shared with the exporter: EXPORT must be the resolver's
    same logic, not a parallel implementation -- proven here by showing a phase that
    happens to declare every scope in the workspace produces the identical result."""
    tenant_id, workspace_id, principals = await _setup("vis-export-parity")
    everything = _visibility(["workspace_public", "facilitator_only"])

    for role in ("facilitator", "participant", "viewer", "agent"):
        phase_scoped = await scopes_for(
            tenant_id, principals[role], workspace_id, everything, session_id=None
        )
        export_scoped = await scopes_for(
            tenant_id, principals[role], workspace_id, EXPORT, session_id=None
        )
        assert phase_scoped == export_scoped


# ── seed_default_scopes idempotency ──────────────────────────────────────────────────


async def test_seed_default_scopes_is_idempotent(db_available: None) -> None:
    tenant_id, workspace_id, _principals = await _setup("vis-idem")
    await seed_default_scopes(tenant_id, workspace_id)  # already seeded by seed_dev_tenant
    await seed_default_scopes(tenant_id, workspace_id)  # calling again must not duplicate/error

    async with tenant_scope(tenant_id) as session:
        keys = (
            (
                await session.execute(
                    select(ScopeRow.key).where(ScopeRow.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
    assert sorted(keys) == ["facilitator_only", "workspace_public"]
