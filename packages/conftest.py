"""Shared fixtures for packages/core, packages/adapters, and packages/api tests that
need a live Postgres and/or Redis. Mirrors tests/isolation/conftest.py's
skip-if-unreachable pattern so ``pytest`` stays runnable without either for tests that
don't need them, while tests that do (permission, job queue, vector store, audit,
idempotency, auth, rate limiting, ...) get exercised for real whenever one is reachable —
including in the ``isolation``/``test`` CI jobs that provision them. Lives at the
``packages/`` root so ``core``, ``adapters``, and ``api`` test suites all pick it up.

``purge_tenants_this_run_created`` keeps the suite runnable: nearly every test here
seeds its own tenant and nothing ever removed one, so a shared development database
accumulated them run after run until a ``count(*)`` inside a test took minutes. See
``core.tenancy.tenant_cleanup`` for why it deletes only what the run itself created.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from core.tenancy.scope import dispose_engine, unscoped_session
from core.tenancy.tenant_cleanup import existing_tenant_ids, purge_tenants


@pytest_asyncio.fixture
async def db_available() -> AsyncIterator[None]:
    try:
        async with unscoped_session() as session:
            await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        pytest.skip(f"no reachable Postgres for adapter contract tests: {exc}")
    yield
    await dispose_engine()


@pytest_asyncio.fixture
async def redis_available() -> AsyncIterator[None]:
    from api.redis_client import close_redis, get_redis

    try:
        await get_redis().ping()
    except (RedisError, OSError) as exc:
        pytest.skip(f"no reachable Redis for rate-limit/streaming tests: {exc}")
    yield
    await close_redis()


@pytest_asyncio.fixture
async def platform_admin_headers(db_available: None, redis_available: None) -> dict[str, str]:
    """A fresh platform-admin login: an owner in the reserved admin tenant, signed in
    through the API exactly as an operator is. The bearer this returns is the only thing
    ``require_platform_admin`` accepts -- there is no shared ops token any more."""
    import uuid

    from fastapi.testclient import TestClient

    from adapters.identity.local.argon2_provider import LocalArgon2IdentityProvider
    from api.main import app
    from api.redis_client import get_redis
    from core.tenancy.admin import ADMIN_TENANT_ID
    from core.tenancy.provisioning import create_tenant_user

    email = f"admin-{uuid.uuid4().hex[:10]}@example.com"
    password = "correct horse battery"
    principal_id = await create_tenant_user(ADMIN_TENANT_ID, "Platform Admin", "owner")
    await LocalArgon2IdentityProvider().register_local(
        ADMIN_TENANT_ID, principal_id, email, password
    )
    await get_redis().delete("ratelimit:ip:testclient")
    with TestClient(app) as client:
        login = client.post(
            "/auth/login",
            json={"email": email, "password": password},
            headers={"X-Pyrrhula-Tenant": "admin"},
        )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest_asyncio.fixture(scope="session", autouse=True)
async def purge_tenants_this_run_created() -> AsyncIterator[None]:
    """See ``core.tenancy.tenant_cleanup``."""
    before = await existing_tenant_ids()
    yield
    await purge_tenants(await existing_tenant_ids() - before)
