"""The steward seat: one person who can both build a workspace and watch it.

A workspace's creator used to be an overseer -- able to manage the room but not author a
secret, knowledge, or a flow in it. So a solo user needed a second account just to write a
case in a workspace they own. steward is the combined facilitator+overseer seat that
closes that, without collapsing the facilitator/overseer split for the multi-human
workspaces where it earns its keep.

These tests fix the two halves of the seat (both permission sets), the visibility
implication (a steward matches a facilitator-only scope), and the Q6 presence check (a
steward is an overseer for "does this workspace have oversight").
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from adapters.moderation.allow_all import AllowAllModerationProvider
from adapters.permission.role_permission import RolePermissionService
from core.assembler.visibility import EXPORT, scopes_for, seed_default_scopes
from core.secrets.authoring import create_secret
from core.tenancy.models import Principal, Workspace, WorkspaceMembership
from core.tenancy.roles import WORKSPACE_ROLES, role_satisfies, roles_satisfying
from core.tenancy.scope import tenant_scope

_PERMISSIONS = RolePermissionService()
_ENCRYPTOR = IdentityEncryptor()
_MODERATION = AllowAllModerationProvider()


def test_a_steward_satisfies_both_seats_and_the_others_stay_distinct() -> None:
    assert role_satisfies("steward", "facilitator")
    assert role_satisfies("steward", "overseer")
    assert role_satisfies("steward", "steward")
    # A facilitator is NOT a steward, and is not an overseer -- the implication is one-way,
    # so a plain facilitator still cannot inspect and the split is preserved.
    assert not role_satisfies("facilitator", "overseer")
    assert not role_satisfies("facilitator", "steward")
    assert not role_satisfies("overseer", "facilitator")
    assert roles_satisfying("overseer") == frozenset({"overseer", "steward"})
    assert roles_satisfying("facilitator") == frozenset({"facilitator", "steward"})
    assert "steward" in WORKSPACE_ROLES


async def _workspace_of(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _member(tenant_id: uuid.UUID, workspace_id: uuid.UUID, role: str) -> Principal:
    async with tenant_scope(tenant_id) as session:
        principal = Principal(tenant_id=tenant_id, kind="human", display_name=f"{role}-person")
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
        await session.flush()
        session.expunge(principal)
        return principal


@pytest.mark.asyncio
async def test_a_steward_has_both_the_author_and_the_inspect_half(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The whole point: one person, both halves. Authoring a secret needs `secret:author`
    (the facilitator half). Reading plaintext through the *audited* overseer path needs
    `secret:inspect` (the overseer half) -- which a plain facilitator does not have, so
    that call is what distinguishes the combined seat from a mere facilitator."""
    from core.overseer.service import OverseerPermissionDeniedError, OverseerService

    tenant_id, _ = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    steward = await _member(tenant_id, workspace_id, "steward")
    facilitator = await _member(tenant_id, workspace_id, "facilitator")

    # Facilitator half: authoring succeeds.
    secret = await create_secret(
        tenant_id,
        workspace_id,
        steward.id,
        subject_kind="workspace",
        subject_id=workspace_id,
        content="the fox doorstop, at seven o'clock",
        gist="the weapon and the hour",
        scope_key="workspace_public",
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
        moderation_provider=_MODERATION,
    )

    overseer_svc = OverseerService(encryptor=_ENCRYPTOR, permission_service=_PERMISSIONS)

    # Overseer half: the steward may inspect through the audited path.
    view = await overseer_svc.inspect(tenant_id, steward.id, workspace_id, secret.id)
    assert view.content == "the fox doorstop, at seven o'clock"

    # And the proof it is a real second half: a plain facilitator, who authored nothing
    # here, is refused the same call -- so steward is genuinely more than facilitator.
    with pytest.raises(OverseerPermissionDeniedError):
        await overseer_svc.inspect(tenant_id, facilitator.id, workspace_id, secret.id)


@pytest.mark.asyncio
async def test_a_plain_participant_can_neither_author_nor_read(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The seat is a grant, not a loophole: nothing about adding steward loosens what a
    participant may do."""
    from core.secrets.authoring import SecretAccessDeniedError

    tenant_id, _ = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    participant = await _member(tenant_id, workspace_id, "participant")

    with pytest.raises(SecretAccessDeniedError):
        await create_secret(
            tenant_id,
            workspace_id,
            participant.id,
            subject_kind="workspace",
            subject_id=workspace_id,
            content="x",
            gist="y",
            scope_key="workspace_public",
            encryptor=_ENCRYPTOR,
            permission_service=_PERMISSIONS,
            moderation_provider=_MODERATION,
        )


@pytest.mark.asyncio
async def test_a_steward_sees_the_facilitator_only_scope(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The default `facilitator_only` scope names {facilitator, supervisor} and predates
    steward, so the implication has to be applied at match time -- a steward reads it, a
    participant does not."""
    tenant_id, _ = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    steward = await _member(tenant_id, workspace_id, "steward")
    participant = await _member(tenant_id, workspace_id, "participant")

    steward_scopes = await scopes_for(tenant_id, steward.id, workspace_id, EXPORT, None)
    participant_scopes = await scopes_for(tenant_id, participant.id, workspace_id, EXPORT, None)

    assert "facilitator_only" in steward_scopes
    assert "workspace_public" in steward_scopes
    assert "facilitator_only" not in participant_scopes


@pytest.mark.asyncio
async def test_a_steward_satisfies_the_oversight_requirement(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Q6: a workspace that needs an overseer present is satisfied by a steward, so a solo
    owner does not have to invent a second human to clear the check."""
    from core.overseer.workspace_requirements import validate_overseer_requirement

    tenant_id, _ = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)

    # Enough humans to trip the multi-human oversight requirement, none of them an
    # overseer by name -- only the steward, who satisfies it.
    await _member(tenant_id, workspace_id, "steward")
    await _member(tenant_id, workspace_id, "participant")
    await _member(tenant_id, workspace_id, "participant")

    # Raises if the requirement is unmet; the steward is why it is met.
    await validate_overseer_requirement(tenant_id, workspace_id)
