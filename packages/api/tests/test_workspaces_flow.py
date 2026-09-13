"""T0.9: list workspaces/agents so the frontend has something real to show."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
from core.agents.seed import seed_dev_agent
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


async def test_list_workspaces_and_agents(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"wslist-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)

    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    ws_resp = client.get("/workspaces", headers=headers)
    assert ws_resp.status_code == 200
    workspaces = ws_resp.json()
    assert any(w["id"] == str(workspace_id) for w in workspaces)

    agents_resp = client.get(f"/workspaces/{workspace_id}/agents", headers=headers)
    assert agents_resp.status_code == 200
    agents = agents_resp.json()
    assert any(a["id"] == str(persona_id) for a in agents)


async def test_workspaces_endpoint_requires_auth(client: TestClient, db_available: None) -> None:
    response = client.get("/workspaces")
    assert response.status_code == 401


async def test_workspaces_are_tenant_isolated(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug_a = f"wslist-a-{uuid.uuid4().hex[:8]}"
    slug_b = f"wslist-b-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug_a)
    await seed_dev_tenant(slug=slug_b)

    token_b = _register_and_login(client, slug_b)
    resp_b = client.get("/workspaces", headers={"Authorization": f"Bearer {token_b}"})
    # `key` is human-readable and legitimately collides across tenants (both get a
    # "default" workspace from seed_dev_tenant) -- id is the actual isolation boundary.
    workspace_ids_b = {w["id"] for w in resp_b.json()}

    token_a = _register_and_login(client, slug_a)
    resp_a = client.get("/workspaces", headers={"Authorization": f"Bearer {token_a}"})
    workspace_ids_a = {w["id"] for w in resp_a.json()}

    assert workspace_ids_a.isdisjoint(workspace_ids_b)
