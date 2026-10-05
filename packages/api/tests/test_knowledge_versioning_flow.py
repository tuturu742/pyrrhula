"""diff/fork/effective-version endpoints over real HTTP."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
from api.tests.grants import grant_tenant_role
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


async def test_diff_endpoint_returns_added_removed_changed(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"kn-diff-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    await grant_tenant_role(client, token, tenant_id, "editor")
    headers = {"Authorization": f"Bearer {token}"}

    source_id = client.post(
        "/knowledge/sources",
        json={"key": "core-rules", "name": "Core Rules", "class": "rules"},
        headers=headers,
    ).json()["id"]

    client.put(
        f"/knowledge/sources/{source_id}/entries/grappling",
        json={
            "title": "Grappling",
            "body_md": "v1",
            "class": "rules",
            "scope_key": "workspace_public",
        },
        headers=headers,
    )
    v1 = client.post(f"/knowledge/sources/{source_id}/publish", json={}, headers=headers).json()

    client.put(
        f"/knowledge/sources/{source_id}/entries/grappling",
        json={
            "title": "Grappling",
            "body_md": "v2",
            "class": "rules",
            "scope_key": "workspace_public",
        },
        headers=headers,
    )
    v2 = client.post(f"/knowledge/sources/{source_id}/publish", json={}, headers=headers).json()

    resp = client.get(
        f"/knowledge/sources/{source_id}/diff",
        params={"from_version_id": v1["id"], "to_version_id": v2["id"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["added"] == []
    assert body["removed"] == []
    assert [c["entry_key"] for c in body["changed"]] == ["grappling"]


async def test_list_versions_endpoint_returns_newest_first(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"kn-list-versions-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    await grant_tenant_role(client, token, tenant_id, "editor")
    headers = {"Authorization": f"Bearer {token}"}

    source_id = client.post(
        "/knowledge/sources",
        json={"key": "core-rules", "name": "Core Rules", "class": "rules"},
        headers=headers,
    ).json()["id"]

    client.put(
        f"/knowledge/sources/{source_id}/entries/grappling",
        json={
            "title": "Grappling",
            "body_md": "v1",
            "class": "rules",
            "scope_key": "workspace_public",
        },
        headers=headers,
    )
    v1 = client.post(f"/knowledge/sources/{source_id}/publish", json={}, headers=headers).json()

    client.put(
        f"/knowledge/sources/{source_id}/entries/grappling",
        json={
            "title": "Grappling",
            "body_md": "v2",
            "class": "rules",
            "scope_key": "workspace_public",
        },
        headers=headers,
    )
    v2 = client.post(
        f"/knowledge/sources/{source_id}/publish",
        json={"change_note": "tweaked grappling"},
        headers=headers,
    ).json()

    resp = client.get(f"/knowledge/sources/{source_id}/versions", headers=headers)
    assert resp.status_code == 200, resp.text
    versions = resp.json()
    assert [v["id"] for v in versions] == [v2["id"], v1["id"]]  # newest first
    assert versions[0]["change_note"] == "tweaked grappling"
    assert versions[0]["version_number"] == 2
    assert versions[1]["version_number"] == 1


async def test_fork_endpoint_creates_independent_source(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"kn-fork-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    await grant_tenant_role(client, token, tenant_id, "editor")
    headers = {"Authorization": f"Bearer {token}"}

    source_id = client.post(
        "/knowledge/sources",
        json={"key": "core-rules", "name": "Core Rules", "class": "rules"},
        headers=headers,
    ).json()["id"]
    client.put(
        f"/knowledge/sources/{source_id}/entries/grappling",
        json={
            "title": "Grappling",
            "body_md": "v1",
            "class": "rules",
            "scope_key": "workspace_public",
        },
        headers=headers,
    )
    version = client.post(
        f"/knowledge/sources/{source_id}/publish", json={}, headers=headers
    ).json()

    fork_resp = client.post(
        f"/knowledge/sources/{source_id}/fork",
        json={"from_version_id": version["id"], "new_key": "core-rules-fork", "new_name": "Fork"},
        headers=headers,
    )
    assert fork_resp.status_code == 201, fork_resp.text
    forked = fork_resp.json()
    assert forked["id"] != source_id
    assert forked["current_version_id"] is not None


async def test_effective_version_endpoint_follows_latest_by_default(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"kn-effective-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    await grant_tenant_role(client, token, tenant_id, "editor")
    headers = {"Authorization": f"Bearer {token}"}

    source_id = client.post(
        "/knowledge/sources",
        json={"key": "core-rules", "name": "Core Rules", "class": "rules"},
        headers=headers,
    ).json()["id"]
    client.put(
        f"/knowledge/sources/{source_id}/entries/grappling",
        json={
            "title": "Grappling",
            "body_md": "v1",
            "class": "rules",
            "scope_key": "workspace_public",
        },
        headers=headers,
    )
    version = client.post(
        f"/knowledge/sources/{source_id}/publish", json={}, headers=headers
    ).json()

    resp = client.get(
        f"/knowledge/workspaces/{workspace_id}/sources/{source_id}/effective-version",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version_id"] == version["id"]
