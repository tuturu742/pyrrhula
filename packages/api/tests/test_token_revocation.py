"""Trust-ops: logout must actually END a session. A stateless JWT is valid until its
own expiry, so logout revokes the presented token's jti (Redis denylist, TTL'd to that
token's exp) and the auth middleware refuses it from then on."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
from core.tenancy.seed import seed_dev_tenant


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture
def client():  # noqa: ANN201
    with TestClient(app) as test_client:
        yield test_client


async def test_logout_revokes_the_presented_token(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"revoke-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    email = f"{uuid.uuid4().hex}@example.com"
    register = client.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery", "display_name": "T"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert register.status_code == 200, register.text
    token = register.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    assert client.get("/me", headers=auth).status_code == 200

    assert client.post("/auth/logout", headers=auth).status_code == 200

    # The very same token is now refused -- not merely forgotten by the browser.
    resp = client.get("/me", headers=auth)
    assert resp.status_code == 401, resp.text
    assert "revoked" in resp.text
