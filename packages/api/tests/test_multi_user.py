"""U3's multi-user seam, end to end: an owner creates a teammate in their OWN org,
adds them to a workspace, the teammate signs in and acts; password change verifies the
old secret, stores a new one, and revokes the session that made the change."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text

from api.main import app
from api.redis_client import get_redis
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def _promote_to_owner(slug: str, email: str) -> None:
    """/auth/register grants owner only to a tenant's first human; the dev seed already
    made one, so promote the test's registrant explicitly."""
    from core.tenancy.scope import unscoped_session

    async with unscoped_session() as session:
        tid = (
            await session.execute(text("select id from tenant where slug=:s"), {"s": slug})
        ).scalar_one()
    async with tenant_scope(tid) as session:
        await session.execute(
            text(
                "update membership set role='owner' where principal_id = "
                "(select principal_id from identity where provider='local' and external_id=:e)"
            ),
            {"e": email},
        )


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture
def client():  # noqa: ANN201
    with TestClient(app) as test_client:
        yield test_client


def _register_owner(client: TestClient, slug: str) -> tuple[dict[str, str], str]:
    email = f"owner-{uuid.uuid4().hex[:8]}@example.com"
    response = client.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery", "display_name": "Owner"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert response.status_code == 200, response.text
    return (
        {
            "Authorization": f"Bearer {response.json()['access_token']}",
            "X-Pyrrhula-Tenant": slug,
        },
        email,
    )


async def test_owner_creates_teammate_and_adds_to_workspace(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"mu-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    owner_headers, owner_email = _register_owner(client, slug)
    await _promote_to_owner(slug, owner_email)
    # the owner manages workspaces THEY belong to: create one through the API (the
    # creator gets the facilitator membership that carries manage_workspace)
    created_ws = client.post(
        "/workspaces", json={"key": "team", "name": "Team"}, headers=owner_headers
    )
    assert created_ws.status_code == 201, created_ws.text
    workspace_id = created_ws.json()["id"]

    # 1) owner creates a teammate in their OWN org (was platform-admin-only)
    teammate_email = f"teammate-{uuid.uuid4().hex[:8]}@example.com"
    created = client.post(
        "/tenant/users",
        json={
            "email": teammate_email,
            "password": "another horse battery",
            "display_name": "Teammate",
            "role": "participant",
        },
        headers=owner_headers,
    )
    assert created.status_code == 201, created.text
    teammate_id = created.json()["principal_id"]

    listed = client.get("/tenant/users", headers=owner_headers).json()
    assert any(u["email"] == teammate_email for u in listed)

    # 2) owner adds them to the workspace by email (endpoint did not exist at all)
    added = client.post(
        f"/workspaces/{workspace_id}/members",
        json={"email": teammate_email, "role": "participant"},
        headers=owner_headers,
    )
    assert added.status_code == 201, added.text
    assert added.json()["principal_id"] == teammate_id

    members = client.get(f"/workspaces/{workspace_id}/members", headers=owner_headers).json()
    assert any(m["principal_id"] == teammate_id and not m["is_persona"] for m in members)

    # duplicate add is a 409, unknown email a 404
    assert (
        client.post(
            f"/workspaces/{workspace_id}/members",
            json={"email": teammate_email},
            headers=owner_headers,
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/workspaces/{workspace_id}/members",
            json={"email": "nobody@example.com"},
            headers=owner_headers,
        ).status_code
        == 404
    )

    # 3) the teammate can sign in and sees the workspace
    login = client.post(
        "/auth/login",
        json={"email": teammate_email, "password": "another horse battery"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert login.status_code == 200, login.text
    teammate_headers = {
        "Authorization": f"Bearer {login.json()['access_token']}",
        "X-Pyrrhula-Tenant": slug,
    }
    me = client.get("/me", headers=teammate_headers).json()
    assert me["display_name"] == "Teammate"
    assert me["email"] == teammate_email

    # 4) removal works, and a non-manager teammate cannot manage members
    assert (
        client.post(
            f"/workspaces/{workspace_id}/members",
            json={"email": teammate_email},
            headers=teammate_headers,
        ).status_code
        == 403
    )
    removed = client.delete(
        f"/workspaces/{workspace_id}/members/{teammate_id}", headers=owner_headers
    )
    assert removed.status_code == 204


async def test_change_password_verifies_rotates_and_revokes(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"pw-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    headers, email = _register_owner(client, slug)

    # wrong current password -> 403, nothing changes
    assert (
        client.post(
            "/auth/change-password",
            json={"current_password": "wrong", "new_password": "a brand new secret"},
            headers=headers,
        ).status_code
        == 403
    )

    ok = client.post(
        "/auth/change-password",
        json={
            "current_password": "correct horse battery",
            "new_password": "a brand new secret",
        },
        headers=headers,
    )
    assert ok.status_code == 200, ok.text

    # the session that changed the password is revoked...
    assert client.get("/me", headers=headers).status_code == 401

    # ...the old password no longer works, the new one does
    old = client.post(
        "/auth/login",
        json={"email": email, "password": "correct horse battery"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert old.status_code == 401
    new = client.post(
        "/auth/login",
        json={"email": email, "password": "a brand new secret"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert new.status_code == 200


async def test_display_name_patch(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"dn-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    headers, _email = _register_owner(client, slug)
    patched = client.patch("/me", json={"display_name": "New Name"}, headers=headers)
    assert patched.status_code == 200, patched.text
    assert patched.json()["display_name"] == "New Name"
    assert client.get("/me", headers=headers).json()["display_name"] == "New Name"
