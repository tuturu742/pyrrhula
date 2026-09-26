"""Acceptance criteria for the moderation layer: the authoring scan reads a secret's
content whatever its disclosure state, the generation hook follows the
regenerate-then-fallback ladder, per-tenant policy produces different outcomes on identical
text, and a multi-human workspace may satisfy the overseer requirement with moderation
instead of an overseer.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from adapters.moderation.allow_all import AllowAllModerationProvider
from adapters.moderation.keyword import KeywordModerationProvider
from adapters.permission.role_permission import RolePermissionService
from core.agents.seed import seed_dev_agent
from core.assembler.visibility import seed_default_scopes
from core.audit.models import AuditLogRow
from core.moderation.hooks import FALLBACK_REPLY, scan_authored, scan_generated
from core.moderation.policy import ModerationPolicy, get_policy, parse_policy, set_policy
from core.overseer.workspace_requirements import (
    OverseerRequiredError,
    validate_overseer_requirement,
)
from core.process.skeleton import create_session
from core.secrets.authoring import create_secret
from core.secrets.models import SecretRow
from core.sessions.models import SessionEventRow
from core.tenancy.models import Principal, Workspace, WorkspaceMembership
from core.tenancy.scope import tenant_scope

_PERMISSIONS = RolePermissionService()
_BANNED = "trebuchet"
_PROVIDER = KeywordModerationProvider(categories={"siegecraft": (_BANNED,)})


async def _workspace_of(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _member(tenant_id: uuid.UUID, workspace_id: uuid.UUID, role: str) -> Principal:
    async with tenant_scope(tenant_id) as session:
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
        await session.flush()
        session.expunge(principal)
        return principal


def test_a_missing_or_malformed_policy_falls_back_to_permissive() -> None:
    """A parse error in a *control* must fail toward the behaviour the tenant already had.
    Failing closed here would mean a typo in settings silently censors a workspace."""
    assert parse_policy({}) == ModerationPolicy()
    assert parse_policy({"moderation": "not a dict"}) == ModerationPolicy()
    assert parse_policy({"moderation": {"enabled": True, "action": "obliterate"}}).action == "flag"
    assert parse_policy({"moderation": {"enabled": True, "action": "block"}}).blocks is True


async def test_authoring_scan_reads_secret_content_regardless_of_disclosure_state(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    author = await _member(tenant_id, workspace_id, "facilitator")
    await set_policy(tenant_id, ModerationPolicy(enabled=True, action="block"))

    secret = await create_secret(
        tenant_id,
        workspace_id,
        author.id,
        subject_kind="entity",
        subject_id=uuid.uuid4(),
        content=f"the plan is to build a {_BANNED} in the orchard",
        gist="a plan",
        scope_key="workspace_public",
        encryptor=IdentityEncryptor(),
        permission_service=_PERMISSIONS,
        moderation_provider=AllowAllModerationProvider(),
    )

    # `undisclosed` is the default, and is exactly the state that would blind a scanner
    # which respected disclosure -- nothing has been said to anyone, so a
    # disclosure-respecting scan would see nothing. The point is that this one does not.
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SecretRow, secret.id)
        assert row is not None
        assert row.disclosure_state == "undisclosed"
        plaintext = IdentityEncryptor().decrypt(row.content_ciphertext)

    outcome = await scan_authored(
        tenant_id,
        plaintext,
        context="secret",
        target_type="secret",
        target_id=secret.id,
        actor_principal_id=author.id,
        provider=_PROVIDER,
    )
    assert outcome.blocked is True
    assert outcome.action_taken == "block"
    assert outcome.reasons == ("siegecraft",)

    # The audit row names the decision and the target, never the text -- a moderation log
    # full of what it flagged is a second copy of every sensitive thing anyone wrote.
    async with tenant_scope(tenant_id) as session:
        rows = list(
            (
                await session.execute(
                    select(AuditLogRow).where(AuditLogRow.action == "moderation:scan")
                )
            ).scalars()
        )
    assert len(rows) == 1
    assert rows[0].resource_id == secret.id
    assert rows[0].query == {"action_taken": "block", "categories": ["siegecraft"]}
    assert _BANNED not in str(rows[0].query)
    assert "orchard" not in str(rows[0].query)


async def test_generation_moderation_follows_regenerate_then_fallback(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    actor = await _member(tenant_id, workspace_id, "facilitator")
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    await set_policy(tenant_id, ModerationPolicy(enabled=True, action="block"))

    # Rung 1: a clean reply passes untouched and costs no regeneration.
    calls = 0

    async def never() -> str:
        nonlocal calls
        calls += 1
        return "unused"

    text, outcome = await scan_generated(
        tenant_id,
        sess.id,
        1,
        "The gate is open.",
        actor_principal_id=actor.id,
        provider=_PROVIDER,
        regenerate=never,
    )
    assert (text, outcome.action_taken, calls) == ("The gate is open.", "clean", 0)

    # Rung 2: blocked once, regenerated clean.
    async def regenerate_clean() -> str:
        nonlocal calls
        calls += 1
        return "They talk about siege engines in the abstract."

    text, outcome = await scan_generated(
        tenant_id,
        sess.id,
        2,
        f"He describes the {_BANNED} in detail.",
        actor_principal_id=actor.id,
        provider=_PROVIDER,
        regenerate=regenerate_clean,
    )
    assert calls == 1
    assert outcome.action_taken == "regenerated"
    assert _BANNED not in text

    # Rung 3: blocked twice -> fallback + overseer alert, and *never* a third attempt.
    async def regenerate_dirty() -> str:
        nonlocal calls
        calls += 1
        return f"Fine: the {_BANNED} again."

    calls = 0
    text, outcome = await scan_generated(
        tenant_id,
        sess.id,
        3,
        f"The {_BANNED} is loaded.",
        actor_principal_id=actor.id,
        provider=_PROVIDER,
        regenerate=regenerate_dirty,
    )
    assert calls == 1, "regenerate must be called at most once, whatever the outcome"
    assert text == FALLBACK_REPLY
    assert outcome.blocked is True
    assert outcome.action_taken == "fallback"

    async with tenant_scope(tenant_id) as session:
        alerts = list(
            (
                await session.execute(
                    select(SessionEventRow).where(
                        SessionEventRow.session_id == sess.id,
                        SessionEventRow.kind == "moderation_alert",
                    )
                )
            ).scalars()
        )
    assert len(alerts) == 1
    assert alerts[0].payload["categories"] == ["siegecraft"]


async def test_moderation_policy_is_tenant_scoped(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Identical text, two tenants, two outcomes -- which is only meaningful because the
    policy, not the provider, decides."""
    tenant_strict, tenant_permissive = two_tenants
    offending = f"a {_BANNED} at dawn"

    strict_workspace = await _workspace_of(tenant_strict)
    permissive_workspace = await _workspace_of(tenant_permissive)
    await seed_default_scopes(tenant_strict, strict_workspace)
    await seed_default_scopes(tenant_permissive, permissive_workspace)
    strict_author = await _member(tenant_strict, strict_workspace, "facilitator")
    permissive_author = await _member(tenant_permissive, permissive_workspace, "facilitator")

    await set_policy(tenant_strict, ModerationPolicy(enabled=True, action="block"))
    # The permissive tenant never opts in: the default.
    assert (await get_policy(tenant_permissive)).enabled is False

    strict = await scan_authored(
        tenant_strict,
        offending,
        context="knowledge",
        target_type="knowledge_entry",
        target_id=None,
        actor_principal_id=strict_author.id,
        provider=_PROVIDER,
    )
    permissive = await scan_authored(
        tenant_permissive,
        offending,
        context="knowledge",
        target_type="knowledge_entry",
        target_id=None,
        actor_principal_id=permissive_author.id,
        provider=_PROVIDER,
    )

    assert strict.blocked is True
    assert permissive.allowed is True
    assert permissive.action_taken == "skipped", (
        "a disabled policy must make no provider call at all -- a tenant that never opted "
        "in never sends their text anywhere"
    )

    # A third posture: enabled, but flagging rather than blocking. The content goes
    # through, marked. `flag` is not a weaker `block`; it is a different decision.
    await set_policy(tenant_permissive, ModerationPolicy(enabled=True, action="flag"))
    flagged = await scan_authored(
        tenant_permissive,
        offending,
        context="knowledge",
        target_type="knowledge_entry",
        target_id=None,
        actor_principal_id=permissive_author.id,
        provider=_PROVIDER,
    )
    assert flagged.allowed is True
    assert flagged.action_taken == "flag"
    assert flagged.reasons == ("siegecraft",)


async def test_multi_human_workspace_accepts_moderation_in_lieu_of_overseer(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    await _member(tenant_id, workspace_id, "facilitator")
    await _member(tenant_id, workspace_id, "participant")

    # The requirement's first arm: multi-human with no overseer is refused.
    with pytest.raises(OverseerRequiredError, match="overseer"):
        await validate_overseer_requirement(tenant_id, workspace_id)

    # Moderation enabled but *not* overseer-equivalent changes nothing: scanning that
    # nobody reads is not oversight, and the rule asks for oversight.
    await set_policy(tenant_id, ModerationPolicy(enabled=True, action="block"))
    with pytest.raises(OverseerRequiredError):
        await validate_overseer_requirement(tenant_id, workspace_id)

    # The second arm: moderation with overseer-equivalent visibility satisfies it.
    await set_policy(
        tenant_id,
        ModerationPolicy(enabled=True, action="block", overseer_equivalent=True),
    )
    await validate_overseer_requirement(tenant_id, workspace_id)

    # And turning moderation off again re-closes the gate -- the requirement is a live
    # check, not a one-time blessing.
    await set_policy(tenant_id, ModerationPolicy())
    with pytest.raises(OverseerRequiredError):
        await validate_overseer_requirement(tenant_id, workspace_id)
