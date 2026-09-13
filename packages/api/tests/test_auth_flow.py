"""End-to-end auth flow (T0.6): register -> login -> /me, plus the two properties the
brief cares most about -- a valid token for tenant A cannot be pointed at tenant B, and
a disabled principal loses access immediately (not just after token expiry).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text

from api.main import app
from api.redis_client import get_redis
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    # TestClient always presents as client=("testclient", 50000) (Starlette's fixed
    # synthetic test client), so every test in this module shares one rate-limit key
    # against a persistent Redis. Reset it per-test so repeated runs -- the exact
    # scenario that bit the identity adapter tests and the register-flow test data
    # earlier -- don't eventually start returning 429 instead of the status this file
    # actually asserts.
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture
def client() -> Iterator[TestClient]:
    # Used as a context manager (not just constructed) so its internal portal/loop is
    # torn down deterministically at the end of each test, rather than whenever the
    # garbage collector gets to it -- undeterministic teardown timing is what was
    # producing "Event loop is closed" errors from a *previous* test's TestClient
    # finalizing while a later test's DB engine was rebinding to a new loop.
    with TestClient(app) as test_client:
        yield test_client


async def _new_tenant_slug() -> str:
    slug = f"authflow-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    return slug


def _register(client: TestClient, slug: str, email: str, password: str = "correct horse") -> str:
    response = client.post(
        "/auth/register",
        json={"email": email, "password": password, "display_name": "Test User"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


async def test_register_then_me(client: TestClient, db_available: None) -> None:
    slug = await _new_tenant_slug()
    email = f"{uuid.uuid4().hex}@example.com"
    token = _register(client, slug, email)

    response = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "Test User"
    assert body["tenant_id"]


async def test_login_with_correct_password(client: TestClient, db_available: None) -> None:
    slug = await _new_tenant_slug()
    email = f"{uuid.uuid4().hex}@example.com"
    _register(client, slug, email, password="correct horse battery")

    response = client.post(
        "/auth/login",
        json={"email": email, "password": "correct horse battery"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert response.status_code == 200
    assert response.json()["access_token"]


async def test_login_with_wrong_password_fails(client: TestClient, db_available: None) -> None:
    slug = await _new_tenant_slug()
    email = f"{uuid.uuid4().hex}@example.com"
    _register(client, slug, email, password="correct horse battery")

    response = client.post(
        "/auth/login",
        json={"email": email, "password": "wrong password"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert response.status_code == 401


async def test_me_without_token_is_401(client: TestClient, db_available: None) -> None:
    response = client.get("/me")
    assert response.status_code == 401


async def test_me_with_garbage_token_is_401(client: TestClient, db_available: None) -> None:
    response = client.get("/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401


async def test_valid_token_cannot_be_pointed_at_a_different_tenant(
    client: TestClient, db_available: None
) -> None:
    slug_a = await _new_tenant_slug()
    slug_b = await _new_tenant_slug()
    email = f"{uuid.uuid4().hex}@example.com"
    token = _register(client, slug_a, email)

    response = client.get(
        "/me",
        headers={"Authorization": f"Bearer {token}", "X-Pyrrhula-Tenant": slug_b},
    )
    assert response.status_code == 403


async def test_disabled_principal_loses_access_immediately(
    client: TestClient, db_available: None
) -> None:
    slug = await _new_tenant_slug()
    email = f"{uuid.uuid4().hex}@example.com"
    token = _register(client, slug, email)

    # Confirm access works before disabling.
    assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    from api.auth.tokens import verify_token

    claims = verify_token(token)
    async with tenant_scope(claims.tenant_id) as session:
        await session.execute(
            text("UPDATE principal SET disabled_at = now() WHERE id = :id"),
            {"id": claims.principal_id},
        )

    # Same still-unexpired token; disabling took effect without waiting for expiry.
    response = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


async def test_register_duplicate_email_is_409(client: TestClient, db_available: None) -> None:
    slug = await _new_tenant_slug()
    email = f"{uuid.uuid4().hex}@example.com"
    _register(client, slug, email)

    response = client.post(
        "/auth/register",
        json={"email": email, "password": "another password", "display_name": "Someone Else"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert response.status_code == 409


async def test_register_without_tenant_header_and_no_single_tenant_mode_is_400(
    client: TestClient, db_available: None
) -> None:
    response = client.post(
        "/auth/register",
        json={"email": "x@example.com", "password": "correct horse", "display_name": "X"},
    )
    assert response.status_code == 400
