"""Archive / soft-delete: DELETE endpoints hide entities from every list, are gated by a
tenant-role permission, and (for agents) retire them from the scheduler -- without ever
hard-deleting the append-only history behind them (migration c4f2a7e1b9d3)."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.auth.tokens import issue_token
from api.main import app
from api.redis_client import get_redis
from core.agents.scheduling import _personas_with_type
from core.process.dsl.fixtures import MINIMAL_MVP_FLOW
from core.tenancy.seed import seed_dev_tenant


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture
def client():  # noqa: ANN201
    with TestClient(app) as test_client:
        yield test_client


def _register_viewer(client: TestClient, slug: str) -> str:
    email = f"{uuid.uuid4().hex}@example.com"
    resp = client.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery", "display_name": "Viewer"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _owner_setup(client: TestClient, slug: str) -> tuple[dict[str, str], uuid.UUID, str]:
    """Seed a tenant, return (owner auth headers, workspace_id, a model-profile id)."""
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    token = issue_token(principal_id=owner_id, tenant_id=tenant_id, expires_in_seconds=3600)
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post(
        "/model-profiles",
        json={"name": f"echo-{uuid.uuid4().hex[:6]}", "provider": "echo", "model": "echo"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return headers, workspace_id, resp.json()["id"]


def _make_agent(client, headers, workspace_id, profile_id, role="participant"):  # noqa: ANN001
    resp = client.post(
        "/agents",
        json={
            "workspace_id": str(workspace_id),
            "key": f"a-{uuid.uuid4().hex[:6]}",
            "name": f"Persona-{uuid.uuid4().hex[:4]}",
            "persona_type": role,
            "agent_id": str(profile_id),
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_archive_persona_hides_from_list_and_scheduler(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"arch-agent-{uuid.uuid4().hex[:8]}"
    headers, workspace_id, profile_id = await _owner_setup(client, slug)
    tenant_id, _owner, _ws = await seed_dev_tenant(slug=slug)
    keep = _make_agent(client, headers, workspace_id, profile_id, role="participant")
    drop = _make_agent(client, headers, workspace_id, profile_id, role="participant")

    before = {
        a["id"]
        for a in client.get(
            "/agents", params={"workspace_id": str(workspace_id)}, headers=headers
        ).json()
    }
    assert {keep, drop} <= before
    # both participants are scheduler candidates before archiving
    cands_before = await _personas_with_type(tenant_id, workspace_id, "participant")
    assert len(cands_before) == 2

    assert client.delete(f"/agents/{drop}", headers=headers).status_code == 204

    after = {
        a["id"]
        for a in client.get(
            "/agents", params={"workspace_id": str(workspace_id)}, headers=headers
        ).json()
    }
    assert keep in after and drop not in after
    # the archived agent is no longer offered a turn
    cands_after = await _personas_with_type(tenant_id, workspace_id, "participant")
    assert len(cands_after) == 1


async def test_archive_persona_requires_permission(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"arch-perm-{uuid.uuid4().hex[:8]}"
    headers, workspace_id, profile_id = await _owner_setup(client, slug)
    persona_id = _make_agent(client, headers, workspace_id, profile_id)

    viewer = {"Authorization": f"Bearer {_register_viewer(client, slug)}"}
    assert client.delete(f"/agents/{persona_id}", headers=viewer).status_code == 403
    # still there after the denied attempt
    assert client.delete(f"/agents/{persona_id}", headers=headers).status_code == 204


async def test_archive_agent_and_source_and_definition_hidden(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"arch-misc-{uuid.uuid4().hex[:8]}"
    headers, workspace_id, profile_id = await _owner_setup(client, slug)

    # model profile
    assert client.delete(f"/model-profiles/{profile_id}", headers=headers).status_code == 204
    assert profile_id not in {
        p["id"] for p in client.get("/model-profiles", headers=headers).json()
    }

    # knowledge source (+ attach, then archive detaches)
    src = client.post(
        "/knowledge/sources",
        json={"key": f"k-{uuid.uuid4().hex[:6]}", "name": "Lore", "class": "lore"},
        headers=headers,
    ).json()["id"]
    client.post(
        f"/knowledge/sources/{src}/attachments",
        json={"workspace_id": str(workspace_id), "scope_key": "workspace_public"},
        headers=headers,
    )
    assert client.delete(f"/knowledge/sources/{src}", headers=headers).status_code == 204
    assert src not in {s["id"] for s in client.get("/knowledge/sources", headers=headers).json()}
    attachments = client.get(
        f"/knowledge/workspaces/{workspace_id}/attachments", headers=headers
    ).json()
    assert all(a["knowledge_source_id"] != src for a in attachments)

    # process definition
    defn = client.post(
        "/process-definitions",
        json={"key": f"d-{uuid.uuid4().hex[:6]}", "name": "Flow", "definition": MINIMAL_MVP_FLOW},
        headers=headers,
    ).json()["id"]
    assert client.delete(f"/process-definitions/{defn}", headers=headers).status_code == 204
    assert defn not in {d["id"] for d in client.get("/process-definitions", headers=headers).json()}
