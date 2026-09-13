"""Per-persona hosted-git identity over HTTP (G4.17).

The binding table and the resolver shipped without any write path, so every persona acted
under the repo's single token -- which is what prevents a reviewer persona approving a
pull request another persona opened. These cover the endpoints that close that gap, and
the property that matters most: the token goes in and never comes back out.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture(autouse=True)
def _git_store_root(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    """Registering a repo creates its hosted store on disk. Point that at a temp dir so
    the test does not depend on /app/data/blobs existing (or being writable) locally."""
    monkeypatch.setenv("PYRRHULA_MCP_GIT_ROOT", str(tmp_path / "repos"))


@pytest.fixture
def client():  # noqa: ANN201
    with TestClient(app) as test_client:
        yield test_client


def _owner(client: TestClient) -> tuple[dict[str, str], str]:
    """A fresh organization and its owner -- owner is what `repo:manage` needs."""
    org = f"pgc-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/auth/signup",
        json={
            "organization": org,
            "email": f"{uuid.uuid4().hex}@example.com",
            "display_name": "Owner",
            "password": "correct horse battery",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return (
        {
            "Authorization": f"Bearer {body['access_token']}",
            "X-Pyrrhula-Tenant": body["tenant_slug"],
        },
        body["tenant_slug"],
    )


def _a_repo(client: TestClient, headers: dict[str, str]) -> str:
    resp = client.post(
        "/repos",
        json={"key": f"r-{uuid.uuid4().hex[:8]}", "name": "Repo", "access_token": "repo-token"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _a_persona(client: TestClient, headers: dict[str, str]) -> str:
    workspaces = client.get("/workspaces", headers=headers).json()
    workspace_id = workspaces[0]["id"]
    profile = client.post(
        "/model-profiles",
        json={"name": "M", "provider": "openai", "model": "gpt-4o-mini"},
        headers=headers,
    )
    assert profile.status_code in (200, 201), profile.text
    persona = client.post(
        "/agents",
        json={
            "workspace_id": workspace_id,
            "key": f"rev-{uuid.uuid4().hex[:8]}",
            "agent_id": profile.json()["id"],
            "name": "Reviewer",
            "persona_type": "participant",
            "persona_md": "reviews things",
        },
        headers=headers,
    )
    assert persona.status_code in (200, 201), persona.text
    return persona.json()["id"]


def test_bind_list_and_unbind_a_persona_identity(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    headers, _slug = _owner(client)
    repo_id = _a_repo(client, headers)
    persona_id = _a_persona(client, headers)

    # Nothing bound: every persona acts as the repo.
    assert client.get(f"/repos/{repo_id}/persona-credentials", headers=headers).json() == []

    bind = client.put(
        f"/repos/{repo_id}/persona-credentials",
        json={"persona_id": persona_id, "access_token": "ghp_reviewer_secret"},
        headers=headers,
    )
    assert bind.status_code == 200, bind.text
    # The token must never be echoed back, in any field.
    assert "ghp_reviewer_secret" not in bind.text

    listed = client.get(f"/repos/{repo_id}/persona-credentials", headers=headers)
    assert [row["persona_id"] for row in listed.json()] == [persona_id]
    assert "ghp_reviewer_secret" not in listed.text

    # Re-binding rotates rather than colliding on (repo_id, persona_id).
    again = client.put(
        f"/repos/{repo_id}/persona-credentials",
        json={"persona_id": persona_id, "access_token": "ghp_rotated"},
        headers=headers,
    )
    assert again.status_code == 200, again.text
    assert len(client.get(f"/repos/{repo_id}/persona-credentials", headers=headers).json()) == 1

    assert (
        client.delete(
            f"/repos/{repo_id}/persona-credentials/{persona_id}", headers=headers
        ).status_code
        == 204
    )
    assert client.get(f"/repos/{repo_id}/persona-credentials", headers=headers).json() == []


def test_unbinding_nothing_is_a_404(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    headers, _slug = _owner(client)
    repo_id = _a_repo(client, headers)
    resp = client.delete(f"/repos/{repo_id}/persona-credentials/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404, resp.text


def test_binding_on_an_unknown_repo_is_a_404(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    headers, _slug = _owner(client)
    resp = client.put(
        f"/repos/{uuid.uuid4()}/persona-credentials",
        json={"persona_id": str(uuid.uuid4()), "access_token": "t"},
        headers=headers,
    )
    assert resp.status_code == 404, resp.text
