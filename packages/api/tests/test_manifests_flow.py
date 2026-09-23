"""D1.4: per-message ContextManifest is visible over real HTTP, permission-checked
exactly as C1.3's get_manifest_for_message specifies -- the exact viewer always may;
anyone else needs read_any_manifest (facilitator/overseer) on the workspace.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
from core.agents.seed import seed_dev_agent
from core.assembler.context_assembler import ContextManifest, assemble
from core.assembler.manifest import write_context_manifest
from core.knowledge.authoring import create_source
from core.knowledge.retrieval.tests.conftest import (
    attach_to_workspace,
    seed_chunk,
    unit_vector,
)
from core.process.dsl.schema import ActorSpec, BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.skeleton import create_session
from core.sessions.models import MessageRow
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture
def client():  # noqa: ANN201
    with TestClient(app) as test_client:
        yield test_client


def _register_and_login(client: TestClient, slug: str) -> tuple[str, uuid.UUID]:
    email = f"{uuid.uuid4().hex}@example.com"
    response = client.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery", "display_name": "Tester"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    me = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    return token, uuid.UUID(me.json()["principal_id"])


async def _write_manifest_and_message(
    tenant_id: uuid.UUID, session_id: uuid.UUID, event_seq: int, viewer_id: uuid.UUID
) -> uuid.UUID:
    manifest = ContextManifest(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        session_id=session_id,
        rendered_context="<knowledge>grappling rules</knowledge>",
        stable_prefix="",
        volatile_suffix="<knowledge>grappling rules</knowledge>",
        entries=(),
        redactions=(),
        resolution_ids=(),
        token_counts={"total": 12},
        content_hash="a" * 64,
    )
    manifest_row = await write_context_manifest(
        tenant_id, session_id, event_seq, viewer_id, "test_phase", manifest
    )
    async with tenant_scope(tenant_id) as session:
        message = MessageRow(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=event_seq,
            author_principal_id=viewer_id,
            role="assistant",
            content_md="hello",
            context_manifest_id=manifest_row.id,
        )
        session.add(message)
        await session.flush()
        return message.id


async def test_exact_viewer_can_read_their_own_manifest_over_http(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"manifests-viewer-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    token, principal_id = _register_and_login(client, slug)
    message_id = await _write_manifest_and_message(tenant_id, sess.id, 0, principal_id)

    response = client.get(
        f"/messages/{message_id}/manifest", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["viewer_principal_id"] == str(principal_id)
    assert body["token_counts"] == {"total": 12}
    assert body["rendered_hash"] == "a" * 64


async def test_a_bystander_without_read_any_manifest_is_denied(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"manifests-denied-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    viewer_token, viewer_id = _register_and_login(client, slug)
    message_id = await _write_manifest_and_message(tenant_id, sess.id, 0, viewer_id)

    bystander_token, bystander_id = _register_and_login(client, slug)
    async with tenant_scope(tenant_id) as session:
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=bystander_id,
                role="participant",
            )
        )

    response = client.get(
        f"/messages/{message_id}/manifest", headers={"Authorization": f"Bearer {bystander_token}"}
    )
    assert response.status_code == 403, response.text
    assert viewer_token  # the viewer's own token isn't used here, just documents the setup


async def test_a_facilitator_may_read_any_manifest_in_the_workspace(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"manifests-facilitator-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    _viewer_token, viewer_id = _register_and_login(client, slug)
    message_id = await _write_manifest_and_message(tenant_id, sess.id, 0, viewer_id)

    facilitator_token, facilitator_id = _register_and_login(client, slug)
    async with tenant_scope(tenant_id) as session:
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=facilitator_id,
                role="facilitator",
            )
        )

    response = client.get(
        f"/messages/{message_id}/manifest", headers={"Authorization": f"Bearer {facilitator_token}"}
    )
    assert response.status_code == 200, response.text


async def test_manifest_entries_round_trip_class_bucket_rank_score_why_over_http(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    """D1.4's own inspector needs the entry's class/bucket/rank/score/why -- proven via a
    real assemble() output (not a hand-built empty manifest, unlike the other tests
    here), through the actual HTTP response shape."""
    slug = f"manifests-entries-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    await attach_to_workspace(tenant_id, workspace_id, source.id)
    await seed_chunk(
        tenant_id,
        source.id,
        entry_key="grappling",
        body_text="Roll 1d20 plus strength to grapple.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )

    token, principal_id = _register_and_login(client, slug)
    async with tenant_scope(tenant_id) as session:
        viewer = await session.get(Principal, principal_id)
        assert viewer is not None
        # scopes_for() (C1.1) resolves visibility off workspace *role*, not tenant
        # membership -- /auth/register only grants a tenant-level "viewer" role, so
        # assemble() would see zero scopes (and thus zero entries) without this.
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal_id,
                role="participant",
            )
        )

    phase = PhaseSpec(
        label_key="test_phase",
        actors=[ActorSpec(persona_type="supervisor", mode="generate")],
        visibility=VisibilitySpec(
            knowledge_classes=["rules"],
            scopes=["workspace_public"],
            entity_fields="all",
            secrets="none",
        ),
        budget=BudgetSpec(ratio={"rules": 1.0}, max_tokens=500),
    )
    manifest = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=sess.id,
        query_text="grapple",
        query_embedding=unit_vector(0),
        history_max_tokens=0,
    )
    manifest_row = await write_context_manifest(
        tenant_id, sess.id, 0, principal_id, "test_phase", manifest
    )
    async with tenant_scope(tenant_id) as session:
        message = MessageRow(
            tenant_id=tenant_id,
            session_id=sess.id,
            event_seq=0,
            author_principal_id=principal_id,
            role="assistant",
            content_md="You grapple the guard.",
            context_manifest_id=manifest_row.id,
        )
        session.add(message)
        await session.flush()
        message_id = message.id

    response = client.get(
        f"/messages/{message_id}/manifest", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    entries = response.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["entry_key"] == "grappling"
    assert entries[0]["class"] == "rules"
    assert entries[0]["bucket"] == "rules"
    assert isinstance(entries[0]["rank"], int)
    assert isinstance(entries[0]["score"], float)
    assert isinstance(entries[0]["why"], str)
    assert isinstance(entries[0]["token_count"], int)


async def test_manifest_endpoint_requires_auth(client: TestClient, db_available: None) -> None:
    response = client.get(f"/messages/{uuid.uuid4()}/manifest")
    assert response.status_code == 401


async def test_manifest_endpoint_404s_for_a_message_with_no_manifest(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"manifests-none-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    token, principal_id = _register_and_login(client, slug)
    async with tenant_scope(tenant_id) as session:
        message = MessageRow(
            tenant_id=tenant_id,
            session_id=sess.id,
            event_seq=0,
            author_principal_id=principal_id,
            role="assistant",
            content_md="no manifest here",
        )
        session.add(message)
        await session.flush()
        message_id = message.id

    response = client.get(
        f"/messages/{message_id}/manifest", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 404
