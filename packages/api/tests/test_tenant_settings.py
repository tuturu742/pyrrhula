"""Organization preferences over real HTTP: the login lifetime an owner sets is the
lifetime the next login token actually carries, the preview lifetime respects the
operator's ceiling, a member without `manage_tenant` cannot change any of it, and a
stored value that does not parse reads as the default rather than as no limit."""

from __future__ import annotations

import uuid

import jwt
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from api.main import app
from api.redis_client import get_redis
from core.config import get_settings
from core.tenancy.models import Tenant
from core.tenancy.preferences import (
    DEFAULT_PREVIEW_TTL_SECONDS,
    DEFAULT_SESSION_LIFETIME_SECONDS,
    get_preferences,
)
from core.tenancy.scope import tenant_scope, unscoped_session
from core.tenancy.seed import seed_dev_tenant


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture
def client():  # noqa: ANN201
    with TestClient(app) as test_client:
        yield test_client


async def _tenant_id(slug: str) -> uuid.UUID:
    async with unscoped_session() as session:
        return (
            await session.execute(text("select id from tenant where slug=:s"), {"s": slug})
        ).scalar_one()


async def _promote(slug: str, email: str, role: str) -> None:
    tid = await _tenant_id(slug)
    async with tenant_scope(tid) as session:
        await session.execute(
            text(
                "update membership set role=:r where principal_id = "
                "(select principal_id from identity where provider='local' and external_id=:e)"
            ),
            {"r": role, "e": email},
        )


def _register(client: TestClient, slug: str) -> tuple[dict[str, str], str, str]:
    email = f"u-{uuid.uuid4().hex[:8]}@example.com"
    password = "correct horse battery"
    response = client.post(
        "/auth/register",
        json={"email": email, "password": password, "display_name": "U"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert response.status_code == 200, response.text
    headers = {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "X-Pyrrhula-Tenant": slug,
    }
    return headers, email, password


async def _open_tenant(slug: str) -> None:
    from core.tenancy.registration import set_policy

    await set_policy(await _tenant_id(slug), "open")


def _login(client: TestClient, slug: str, email: str, password: str) -> str:
    response = client.post(
        "/auth/login",
        json={"email": email, "password": password},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _lifetime_of(token: str) -> int:
    settings = get_settings()
    claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    return int(claims["exp"]) - int(claims["iat"])


async def test_defaults_then_owner_sets_the_login_lifetime_and_the_next_login_carries_it(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"ts-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    await _open_tenant(slug)
    headers, email, password = _register(client, slug)
    await _promote(slug, email, "owner")

    before = client.get("/tenant/settings", headers=headers)
    assert before.status_code == 200, before.text
    assert before.json()["session_lifetime_seconds"] == DEFAULT_SESSION_LIFETIME_SECONDS
    assert before.json()["preview_ttl_seconds"] == DEFAULT_PREVIEW_TTL_SECONDS
    assert before.json()["reranker_enabled"] is True
    assert _lifetime_of(_login(client, slug, email, password)) == DEFAULT_SESSION_LIFETIME_SECONDS

    changed = client.put(
        "/tenant/settings", json={"session_lifetime_seconds": 8 * 3600}, headers=headers
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["session_lifetime_seconds"] == 8 * 3600
    # An omitted field keeps its value.
    assert changed.json()["preview_ttl_seconds"] == DEFAULT_PREVIEW_TTL_SECONDS

    assert _lifetime_of(_login(client, slug, email, password)) == 8 * 3600


async def test_bounds_are_enforced_and_the_preview_ceiling_is_the_operators(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"ts-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    await _open_tenant(slug)
    headers, email, _ = _register(client, slug)
    await _promote(slug, email, "owner")

    ceiling = get_settings().preview_max_ttl_seconds
    too_long = client.put(
        "/tenant/settings", json={"preview_ttl_seconds": ceiling + 1}, headers=headers
    )
    assert too_long.status_code == 422, too_long.text
    assert "ceiling" in too_long.json()["detail"]

    too_short = client.put(
        "/tenant/settings", json={"session_lifetime_seconds": 10}, headers=headers
    )
    assert too_short.status_code == 422, too_short.text

    nothing = client.put("/tenant/settings", json={}, headers=headers)
    assert nothing.status_code == 422, nothing.text

    ok = client.put(
        "/tenant/settings",
        json={"preview_ttl_seconds": ceiling, "reranker_enabled": False},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["preview_ttl_seconds"] == ceiling
    assert ok.json()["reranker_enabled"] is False
    assert ok.json()["bounds"]["preview_ttl_max"] == ceiling

    prefs = await get_preferences(await _tenant_id(slug))
    assert prefs.reranker_enabled is False and prefs.preview_ttl_seconds == ceiling


async def test_a_member_without_manage_tenant_can_read_but_not_write(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"ts-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    await _open_tenant(slug)
    headers, email, _ = _register(client, slug)
    await _promote(slug, email, "participant")

    assert client.get("/tenant/settings", headers=headers).status_code == 200
    denied = client.put(
        "/tenant/settings", json={"session_lifetime_seconds": 3600}, headers=headers
    )
    assert denied.status_code == 403, denied.text


async def test_an_unparseable_stored_value_reads_as_the_default(
    db_available: None,
) -> None:
    slug = f"ts-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    tid = await _tenant_id(slug)
    ceiling = get_settings().preview_max_ttl_seconds
    async with tenant_scope(tid) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == tid))
        assert tenant is not None
        tenant.settings = {
            **(tenant.settings or {}),
            "session_lifetime_seconds": "forever",
            "preview_ttl_seconds": ceiling * 10,
            "reranker_enabled": "yes",
        }

    prefs = await get_preferences(tid)
    assert prefs.session_lifetime_seconds == DEFAULT_SESSION_LIFETIME_SECONDS
    assert prefs.preview_ttl_seconds == min(DEFAULT_PREVIEW_TTL_SECONDS, ceiling)
    assert prefs.reranker_enabled is True
