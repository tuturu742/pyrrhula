"""The cost/cache-hit dashboard is visible per session via API."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
from core.agents.seed import seed_dev_agent
from core.audit.models import UsageRecordRow
from core.process.skeleton import create_session
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture
def client():  # noqa: ANN201
    with TestClient(app) as test_client:
        yield test_client


def _register_and_login(client: TestClient, slug: str) -> str:
    email = f"{uuid.uuid4().hex}@example.com"
    response = client.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery", "display_name": "Tester"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


async def test_session_usage_endpoint_reports_cache_hit_rate(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"usage-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    async with tenant_scope(tenant_id) as session:
        session.add(
            UsageRecordRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=sess.id,
                provider="echo",
                model="echo-1",
                purpose="generation",
                prompt_tokens=100,
                completion_tokens=20,
                cached_tokens=75,
                estimated_cost=Decimal("0.02"),
            )
        )

    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get(f"/sessions/{sess.id}/usage", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["prompt_tokens"] == 100
    assert body["cached_tokens"] == 75
    assert body["cache_hit_rate"] == pytest.approx(0.75)
    assert Decimal(body["estimated_cost"]) == Decimal("0.02")

    ws_response = client.get(f"/workspaces/{workspace_id}/usage", headers=headers)
    assert ws_response.status_code == 200
    assert ws_response.json()["prompt_tokens"] == 100


async def test_message_usage_endpoint_reports_the_owning_messages_spend(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    """per-message token spend, distinct from another message's in the same
    session -- proves UsageRecordRow.message_id actually discriminates, not just that
    the column exists."""
    slug = f"usage-msg-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    async with tenant_scope(tenant_id) as session:
        from core.sessions.models import MessageRow

        message_a = MessageRow(
            tenant_id=tenant_id,
            session_id=sess.id,
            event_seq=0,
            author_principal_id=uuid.uuid4(),
            role="assistant",
            content_md="reply A",
        )
        message_b = MessageRow(
            tenant_id=tenant_id,
            session_id=sess.id,
            event_seq=1,
            author_principal_id=uuid.uuid4(),
            role="assistant",
            content_md="reply B",
        )
        session.add_all([message_a, message_b])
        await session.flush()
        session.add_all(
            [
                UsageRecordRow(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    session_id=sess.id,
                    message_id=message_a.id,
                    provider="echo",
                    model="echo-1",
                    purpose="generation",
                    prompt_tokens=100,
                    completion_tokens=20,
                    cached_tokens=75,
                    estimated_cost=Decimal("0.02"),
                ),
                UsageRecordRow(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    session_id=sess.id,
                    message_id=message_b.id,
                    provider="echo",
                    model="echo-1",
                    purpose="generation",
                    prompt_tokens=9,
                    completion_tokens=1,
                    cached_tokens=0,
                    estimated_cost=Decimal("0.001"),
                ),
            ]
        )
        message_a_id = message_a.id

    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get(f"/messages/{message_a_id}/usage", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["prompt_tokens"] == 100
    assert body["cached_tokens"] == 75
    assert body["cache_hit_rate"] == pytest.approx(0.75)


async def test_usage_endpoints_require_auth(client: TestClient, db_available: None) -> None:
    assert client.get(f"/sessions/{uuid.uuid4()}/usage").status_code == 401
    assert client.get(f"/workspaces/{uuid.uuid4()}/usage").status_code == 401
    assert client.get(f"/messages/{uuid.uuid4()}/usage").status_code == 401
