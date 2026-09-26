"""Self-serve organization signup is a console switch, not a redeploy: the platform
admin flips it, `/auth/config` reports it, and `/auth/signup` obeys it at once."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _signup(client: TestClient) -> int:
    return client.post(
        "/auth/signup",
        json={
            "organization": f"Org {uuid.uuid4().hex[:6]}",
            "email": f"{uuid.uuid4().hex[:8]}@example.com",
            "password": "correct horse battery",
            "display_name": "Founder",
        },
    ).status_code


async def test_the_console_switch_gates_signup_and_the_public_config_reports_it(
    client: TestClient,
    db_available: None,
    redis_available: None,
    platform_admin_headers: dict[str, str],
) -> None:
    before = client.get("/admin/signup", headers=platform_admin_headers)
    assert before.status_code == 200, before.text
    original = before.json()["allowed"]
    try:
        off = client.put("/admin/signup", json={"allowed": False}, headers=platform_admin_headers)
        assert off.status_code == 200, off.text
        assert off.json()["allowed"] is False
        assert client.get("/auth/config").json()["allow_signup"] is False
        assert _signup(client) == 403

        on = client.put("/admin/signup", json={"allowed": True}, headers=platform_admin_headers)
        assert on.status_code == 200, on.text
        assert client.get("/auth/config").json()["allow_signup"] is True
        assert _signup(client) == 200
    finally:
        client.put("/admin/signup", json={"allowed": original}, headers=platform_admin_headers)


def test_the_switch_is_a_platform_admins_alone(client: TestClient, db_available: None) -> None:
    assert client.get("/admin/signup").status_code == 401
    assert client.put("/admin/signup", json={"allowed": False}).status_code == 401
