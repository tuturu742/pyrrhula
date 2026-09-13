"""Trust mode: the holder's own briefs in its context, the model's judgement on what
to say -- and the line that must not move when a workspace chooses it.

The three secret modes are trust levels over the same structural floor. What trust mode
delegates is what a persona SAYS about its own secrets; what it must never touch is
who-knows-what: another persona's secret entering my context would not be a softer
setting, it would be the product's central claim failing.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from adapters.encryptor.identity import IdentityEncryptor
from adapters.moderation.allow_all import AllowAllModerationProvider
from adapters.permission.role_permission import RolePermissionService
from core.agents.authoring import create_agent, create_persona
from core.assembler.secrets_gate_factory import resolve_trusted_secrets
from core.assembler.visibility import seed_default_scopes
from core.process.dsl.schema import VisibilitySpec
from core.secrets.authoring import add_holder, create_secret
from core.secrets.exclusion import ResolvedSecretDecision, render_injection
from core.secrets.models import DisclosureDecisionRow
from core.tenancy.models import Principal, Workspace, WorkspaceMembership
from core.tenancy.scope import tenant_scope

_ENCRYPTOR = IdentityEncryptor()
_PERMISSIONS = RolePermissionService()
_MODERATION = AllowAllModerationProvider()

_ELIN_BRIEF = "You struck her with the fox doorstop at seven and took page six."
_VIKTOR_BRIEF = "You falsified the receivables and read her mail every evening."


@pytest.fixture
async def two_tenants(db_available: None) -> tuple[uuid.UUID, uuid.UUID]:
    """The leak conftest ships only db_available; the isolation suite's two-tenant
    seeder is reproduced here rather than imported across suites (each owns its own
    fixtures, per the conftest convention)."""
    from core.tenancy.seed import seed_dev_tenant

    suffix = uuid.uuid4().hex[:8]
    tenant_a, _, _ = await seed_dev_tenant(slug=f"trust-a-{suffix}")
    tenant_b, _, _ = await seed_dev_tenant(slug=f"trust-b-{suffix}")
    return tenant_a, tenant_b


class _Phase:
    visibility = VisibilitySpec(
        knowledge_classes=["lore"],
        scopes=["workspace_public"],
        entity_fields="all",
        secrets="held_by_actor",
    )


async def _workspace_of(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _cast(tenant_id: uuid.UUID, workspace_id: uuid.UUID):
    """Two personas, one secret each, held only by its own persona."""
    async with tenant_scope(tenant_id) as session:
        author = Principal(tenant_id=tenant_id, kind="human", display_name="referee")
        session.add(author)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=author.id,
                role="steward",
            )
        )
        await session.flush()
        session.expunge(author)

    connection = await create_agent(
        tenant_id, f"conn-{uuid.uuid4().hex[:6]}", "none", "x", encryptor=_ENCRYPTOR
    )
    personas = {}
    for key, brief in (("elin", _ELIN_BRIEF), ("viktor", _VIKTOR_BRIEF)):
        persona = await create_persona(
            tenant_id,
            workspace_id,
            key,
            key.title(),
            connection.id,
            persona_type="participant",
        )
        secret = await create_secret(
            tenant_id,
            workspace_id,
            author.id,
            subject_kind="agent",
            subject_id=persona.id,
            content=brief,
            gist=f"{key}'s private brief",
            scope_key="workspace_public",
            encryptor=_ENCRYPTOR,
            permission_service=_PERMISSIONS,
            moderation_provider=_MODERATION,
            behavioral_directive="Do not get caught.",
        )
        await add_holder(
            tenant_id,
            secret.id,
            author.id,
            persona.principal_id,
            "author",
            permission_service=_PERMISSIONS,
        )
        personas[key] = persona
    return personas


async def _bare_session(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, persona_id: uuid.UUID
) -> uuid.UUID:
    """resolve_trusted_secrets records a DisclosureDecisionRow, which FKs a session --
    honesty has a foreign key."""
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(
                text(
                    "INSERT INTO session (tenant_id, workspace_id, persona_id,"
                    " current_phase, status, state, next_event_seq)"
                    " VALUES (:t, :w, :p, 'discussion', 'active', '{}'::jsonb, 0)"
                    " RETURNING id"
                ).bindparams(t=tenant_id, w=workspace_id, p=persona_id)
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_trust_mode_gives_a_persona_its_own_brief_and_never_anothers(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The whole mode in one assertion pair. Elin's context gets Elin's brief with the
    directive; Viktor's brief is nowhere in it -- not because a gate concealed it, but
    because the holder query never loaded it. That is the floor trust mode stands on."""
    tenant_id, _ = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    personas = await _cast(tenant_id, workspace_id)
    elin = personas["elin"]
    session_id = await _bare_session(tenant_id, workspace_id, elin.id)

    resolved, concealed = await resolve_trusted_secrets(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        event_seq=0,
        holder_principal_id=elin.principal_id,
        persona_id=elin.id,
        behavior_profile_version=0,
        phase=_Phase(),
        encryptor=_ENCRYPTOR,
    )

    assert concealed == (), "trust mode has nothing concealed -- nothing to post-check"
    rendered = "\n".join(render_injection(r)[0] for r in resolved)
    assert _ELIN_BRIEF in rendered, "the holder's own brief must reach its context"
    assert "Do not get caught." in rendered, "the directive travels with the brief"
    assert _VIKTOR_BRIEF not in rendered, (
        "another persona's secret reached this context: trust mode moved the "
        "who-knows-what line, which no mode may do"
    )


@pytest.mark.asyncio
async def test_trust_mode_records_its_choice(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """No gate ran, but the inclusion is not invisible: a DisclosureDecisionRow with
    action 'trusted' says exactly what happened, so the Director's View can show it and
    an audit can find it. No usage record -- no model ran, nothing to meter."""
    tenant_id, _ = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    personas = await _cast(tenant_id, workspace_id)
    elin = personas["elin"]
    session_id = await _bare_session(tenant_id, workspace_id, elin.id)

    resolved, _ = await resolve_trusted_secrets(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        event_seq=3,
        holder_principal_id=elin.principal_id,
        persona_id=elin.id,
        behavior_profile_version=0,
        phase=_Phase(),
        encryptor=_ENCRYPTOR,
    )
    assert resolved and all(r.action == "trusted" for r in resolved)

    async with tenant_scope(tenant_id) as session:
        row = (
            await session.execute(
                select(DisclosureDecisionRow).where(DisclosureDecisionRow.session_id == session_id)
            )
        ).scalar_one()
    assert row.agent_id is None, "no gate model ran; the row must not claim one did"
    assert all(d["action"] == "trusted" for d in row.decisions)

    usage = None
    async with tenant_scope(tenant_id) as session:
        usage = await session.scalar(
            text("SELECT count(*) FROM usage_record WHERE session_id = :s").bindparams(s=session_id)
        )
    assert usage == 0, "trust mode costs nothing and must not meter a phantom gate call"


def test_the_renderer_marks_trusted_inclusion_honestly() -> None:
    """The manifest's record of a trusted inclusion says what it is -- the same honesty
    convention the eval arms use -- and an unknown action still fails closed."""
    trusted = ResolvedSecretDecision(
        secret_id=uuid.uuid4(),
        action="trusted",
        decision_id=None,
        content="the fact",
        hint_text=None,
        behavioral_directive="play it cool",
    )
    text_out, redaction = render_injection(trusted)
    assert "the fact" in text_out and "play it cool" in text_out
    assert redaction is not None and redaction.reason == "trusted_to_model"

    unknown = ResolvedSecretDecision(
        secret_id=uuid.uuid4(),
        action="banana",
        decision_id=None,
        content="the fact",
        hint_text=None,
        behavioral_directive="d",
    )
    text_out, redaction = render_injection(unknown)
    assert "the fact" not in text_out, "an unrecognised action must still fail closed"
