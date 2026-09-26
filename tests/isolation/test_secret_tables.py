"""Isolation negative tests for the four secret schema tables.

Cross-tenant filter omission and the append-only grant catalog check are also covered
generically by ``test_filter_omission_matrix.py``/``test_coverage_guard.py`` and
``test_append_only_grants.py`` respectively (both extended in this PR to name these four
tables) -- the tests here additionally prove the specific claims the acceptance
criteria name: a secret attaches to all four subject kinds through one polymorphic column
pair, and an UPDATE/DELETE against an append-only table is rejected *in practice*, not
just absent from the grant catalog.

The library-tenant matrix does not apply here: unlike knowledge sources,
secrets have no cross-tenant "shared library" concept -- every secret belongs
to exactly one tenant's one workspace, so there is nothing for a library-matrix test to
exercise.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from adapters.encryptor.identity import IdentityEncryptor
from adapters.moderation.allow_all import AllowAllModerationProvider
from adapters.permission.role_permission import RolePermissionService
from core.agents.authoring import create_agent
from core.agents.seed import seed_dev_agent
from core.process.skeleton import create_session
from core.secrets.authoring import add_holder, create_secret
from core.secrets.decisions import record_disclosure_event, record_gate_decision
from core.secrets.repo import list_secrets_for_subject
from core.tenancy.models import WorkspaceMembership
from core.tenancy.scope import tenant_scope

_ENCRYPTOR = IdentityEncryptor()
_PERMISSIONS = RolePermissionService()
_MODERATION = AllowAllModerationProvider()


async def _grant_author(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, principal_id: uuid.UUID
) -> None:
    """`create_secret`/`add_holder` are permission-gated -- `secret:author` on the
    workspace, granted to the 'facilitator' role (622a637f3fe0). Idempotent per (tenant,
    workspace, principal) via the same unique constraint `WorkspaceMembership` itself
    carries."""
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.principal_id == principal_id,
            )
        )
        if existing is not None:
            return
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal_id,
                role="facilitator",
            )
        )


async def _seed_secret(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, owner_id: uuid.UUID, *, subject_kind: str
) -> uuid.UUID:
    subject_id = workspace_id if subject_kind == "workspace" else uuid.uuid4()
    row = await create_secret(
        tenant_id,
        workspace_id,
        owner_id,
        subject_kind=subject_kind,
        subject_id=subject_id,
        content="the plaintext fact",
        gist="a one-liner gist",
        scope_key="workspace_public",
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
        moderation_provider=_MODERATION,
        behavioral_directive="act nervous around this topic",
    )
    return row.id


async def test_secret_attaches_to_all_four_subject_kinds(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    async with tenant_scope(tenant_a) as session:
        owner_id = (await session.execute(text("SELECT id FROM principal LIMIT 1"))).scalar_one()
        workspace_id = (
            await session.execute(text("SELECT id FROM workspace LIMIT 1"))
        ).scalar_one()
    await _grant_author(tenant_a, workspace_id, owner_id)

    subject_ids: dict[str, uuid.UUID] = {}
    for subject_kind in ("entity", "agent", "workspace", "knowledge_entry"):
        secret_id = await _seed_secret(tenant_a, workspace_id, owner_id, subject_kind=subject_kind)
        subject_ids[subject_kind] = secret_id

    for subject_kind, secret_id in subject_ids.items():
        async with tenant_scope(tenant_a) as session:
            row = (
                await session.execute(
                    text("SELECT subject_kind FROM secret WHERE id = :id"), {"id": secret_id}
                )
            ).scalar_one()
        assert row == subject_kind

    # No per-kind table variants: the same repo function, the same table, the same
    # polymorphic (subject_kind, subject_id) pair for all four.
    matches = await list_secrets_for_subject(tenant_a, "workspace", workspace_id)
    assert {row.id for row in matches} == {subject_ids["workspace"]}


async def test_secret_tables_cross_tenant_filter_omission_returns_zero_rows(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants

    secret_ids: dict[uuid.UUID, uuid.UUID] = {}
    session_ids: dict[uuid.UUID, uuid.UUID] = {}
    for tenant_id in (tenant_a, tenant_b):
        async with tenant_scope(tenant_id) as session:
            owner_id = (
                await session.execute(text("SELECT id FROM principal LIMIT 1"))
            ).scalar_one()
            workspace_id = (
                await session.execute(text("SELECT id FROM workspace LIMIT 1"))
            ).scalar_one()
        await _grant_author(tenant_id, workspace_id, owner_id)

        secret_id = await _seed_secret(tenant_id, workspace_id, owner_id, subject_kind="entity")
        secret_ids[tenant_id] = secret_id

        await add_holder(
            tenant_id, secret_id, owner_id, owner_id, "author", permission_service=_PERMISSIONS
        )

        persona_id = await seed_dev_agent(tenant_id, workspace_id)
        sess = await create_session(tenant_id, workspace_id, persona_id)
        session_ids[tenant_id] = sess.id
        agent = await create_agent(tenant_id, "gate-test", "echo", "echo-1", encryptor=_ENCRYPTOR)

        decision = await record_gate_decision(
            tenant_id,
            sess.id,
            0,
            persona_id,
            1,
            [
                {
                    "secret_id": str(secret_id),
                    "action": "conceal",
                    "rationale": "x",
                    "confidence": 0.9,
                }
            ],
            agent_id=agent.id,
            provider="echo",
            model="echo-1",
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=1,
        )
        await record_disclosure_event(
            tenant_id,
            secret_id,
            sess.id,
            0,
            "hint",
            {"principal_ids": [str(owner_id)]},
            disclosed_by_principal_id=owner_id,
            decision_id=decision.id,
        )

    for table in ("secret", "secret_holder", "secret_disclosure_event", "disclosure_decision"):
        async with tenant_scope(tenant_a) as session:
            rows = (await session.execute(text(f"SELECT tenant_id FROM {table}"))).all()
        assert {row[0] for row in rows} == {tenant_a}, f"{table} leaked tenant_b rows"


async def test_disclosure_tables_reject_update_and_delete_as_app_role(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    async with tenant_scope(tenant_a) as session:
        owner_id = (await session.execute(text("SELECT id FROM principal LIMIT 1"))).scalar_one()
        workspace_id = (
            await session.execute(text("SELECT id FROM workspace LIMIT 1"))
        ).scalar_one()
    await _grant_author(tenant_a, workspace_id, owner_id)
    secret_id = await _seed_secret(tenant_a, workspace_id, owner_id, subject_kind="entity")
    persona_id = await seed_dev_agent(tenant_a, workspace_id)
    sess = await create_session(tenant_a, workspace_id, persona_id)
    agent = await create_agent(tenant_a, "gate-test-2", "echo", "echo-1", encryptor=_ENCRYPTOR)

    decision = await record_gate_decision(
        tenant_a,
        sess.id,
        0,
        persona_id,
        1,
        [],
        agent_id=agent.id,
        provider="echo",
        model="echo-1",
        prompt_tokens=1,
        completion_tokens=1,
        latency_ms=1,
    )
    event = await record_disclosure_event(
        tenant_a, secret_id, sess.id, 0, "hint", {}, decision_id=decision.id
    )

    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_scope(tenant_a) as session:
            await session.execute(
                text("UPDATE disclosure_decision SET latency_ms = 1 WHERE id = :id"),
                {"id": decision.id},
            )

    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_scope(tenant_a) as session:
            await session.execute(
                text("DELETE FROM disclosure_decision WHERE id = :id"), {"id": decision.id}
            )

    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_scope(tenant_a) as session:
            await session.execute(
                text("UPDATE secret_disclosure_event SET mode = 'leaked' WHERE id = :id"),
                {"id": event.id},
            )

    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_scope(tenant_a) as session:
            await session.execute(
                text("DELETE FROM secret_disclosure_event WHERE id = :id"), {"id": event.id}
            )
