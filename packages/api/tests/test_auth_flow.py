"""End-to-end auth flow: register -> login -> /me, plus the two properties the
brief cares most about -- a valid token for tenant A cannot be pointed at tenant B, and
a disabled principal loses access immediately (not just after token expiry).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select, text

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


async def _async(value):  # noqa: ANN001, ANN202 -- a coroutine wrapper for a fixed value
    return value


def _tenant_of(token: str) -> str:
    """The tenant a token was issued for -- login returns the token, not the slug."""
    import base64
    import json

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return str(json.loads(base64.urlsafe_b64decode(payload))["tenant_id"])


async def _new_tenant_slug(policy: str = "open") -> str:
    """A fresh tenant, open to self-registration unless the test says otherwise.

    The deployment default is `closed`, so these tests -- which are about what
    registration DOES, not about who is allowed to -- say so out loud rather than
    relying on a default that must stay permissive for them to pass.
    """
    slug = f"authflow-{uuid.uuid4().hex[:8]}"
    tenant_id, _workspace_id, _owner = await seed_dev_tenant(slug=slug)
    from core.tenancy.registration import set_policy

    await set_policy(tenant_id, policy)
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


# ── single-tenant mode  ────────────────────────────────────────────
#
# Compose and Kubernetes both switch this on by default, and it had never been exercised:
# `default_tenant_slug` defaulted to "dev", a tenant no installer creates, so a deployment
# that advertised "no tenant header required" answered every header-less login with
# `404 unknown tenant: 'dev'`. Verified against a real compose install before this fix.


@pytest.fixture
def single_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "single_tenant_ui", True)
    monkeypatch.setattr(settings, "default_tenant_slug", "")


async def test_single_tenant_mode_resolves_the_one_tenant_without_a_header(
    client: TestClient, db_available: None, single_tenant: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the mode promises: a solo deployment needs no organization field."""
    slug = await _new_tenant_slug()
    _register(client, slug, "solo@example.com")

    from api.middleware import tenant as tenant_middleware
    from core.tenancy.models import Tenant
    from core.tenancy.scope import unscoped_session

    async with unscoped_session() as session:
        sole = await session.scalar(select(Tenant).where(Tenant.slug == slug))
        session.expunge(sole)
    # The shared test database holds every other test's tenants, so the real "is there
    # exactly one?" query cannot be exercised here -- its refusal-to-guess half is, below.
    monkeypatch.setattr(tenant_middleware, "_sole_tenant", lambda: _async(sole))

    response = client.post(
        "/auth/login", json={"email": "solo@example.com", "password": "correct horse"}
    )
    assert response.status_code == 200, response.text
    assert _tenant_of(response.json()["access_token"]) == str(sole.id)


async def test_a_configured_slug_that_does_not_exist_falls_back_to_the_one_tenant(
    client: TestClient, db_available: None, single_tenant: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact shipped landmine: PYRRHULA_DEFAULT_TENANT_SLUG=dev on a box with no
    `dev` tenant. An operator's stale slug should not be a dead end when the deployment
    has exactly one organization to mean."""
    slug = await _new_tenant_slug()
    _register(client, slug, "stale@example.com")

    from api.middleware import tenant as tenant_middleware
    from core.config import get_settings
    from core.tenancy.models import Tenant
    from core.tenancy.scope import unscoped_session

    monkeypatch.setattr(get_settings(), "default_tenant_slug", "dev")
    async with unscoped_session() as session:
        sole = await session.scalar(select(Tenant).where(Tenant.slug == slug))
        session.expunge(sole)
    monkeypatch.setattr(tenant_middleware, "_sole_tenant", lambda: _async(sole))

    response = client.post(
        "/auth/login", json={"email": "stale@example.com", "password": "correct horse"}
    )
    assert response.status_code == 200, response.text


async def test_single_tenant_mode_refuses_to_guess_between_several(
    client: TestClient, db_available: None, single_tenant: None
) -> None:
    """The safety half, and the one the shared test database exercises for real: it holds
    many tenants, so inference must decline rather than sign somebody into whichever row
    came back first."""
    slug = await _new_tenant_slug()
    await _new_tenant_slug()
    _register(client, slug, "ambiguous@example.com")

    response = client.post(
        "/auth/login", json={"email": "ambiguous@example.com", "password": "correct horse"}
    )
    assert response.status_code == 400
    assert "more than one" in response.json()["detail"]


async def test_an_explicit_header_still_wins_in_single_tenant_mode(
    client: TestClient, db_available: None, single_tenant: None
) -> None:
    """Inference is a fallback, never an override: a caller that names a tenant gets it."""
    slug = await _new_tenant_slug()
    _register(client, slug, "explicit@example.com")

    response = client.post(
        "/auth/login",
        json={"email": "explicit@example.com", "password": "correct horse"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert response.status_code == 200, response.text


# ── what the pre-auth screens are allowed to know ────────────────────────────────────


async def test_public_config_reports_the_deployment_shape(
    client: TestClient, db_available: None
) -> None:
    """The login form cannot hide the organization field without being told to.

    Single-tenant mode shipped as half a feature for exactly this reason: the API
    stopped requiring an organization name and the UI went on asking for one, because
    nothing carried the fact across."""
    response = client.get("/auth/config")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"single_tenant", "allow_signup", "has_organization"}
    assert all(isinstance(v, bool) for v in body.values())


async def test_public_config_needs_no_token(client: TestClient, db_available: None) -> None:
    """It is read by the sign-in page, so requiring auth would be circular."""
    assert client.get("/auth/config").status_code == 200


async def test_public_config_reports_what_a_caller_may_omit_not_the_raw_setting(
    client: TestClient, db_available: None, single_tenant: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`single_tenant` answers "may I leave the organization out?", which is the setting
    AND a deployment that has at most one organization. With the setting on and one
    organization, that is yes."""
    from api.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "_organization_count", lambda: _async(1))
    assert client.get("/auth/config").json()["single_tenant"] is True


async def test_public_config_sees_an_organization_once_one_exists(
    client: TestClient, db_available: None
) -> None:
    """`has_organization` is what decides whether the register page offers "create your
    organization" or "join the one that is here" -- getting it wrong in a solo
    deployment means a second organization, which is the one thing that breaks
    single-tenant inference."""
    await _new_tenant_slug()
    assert client.get("/auth/config").json()["has_organization"] is True


# ── who administers the deployment (api.auth.platform_admin) ─────────────────────────


async def test_single_tenant_owner_is_also_the_platform_admin(
    client: TestClient, db_available: None, single_tenant: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One person, one account, both roles.

    Choosing an embedding model lives in the admin console, so without this a solo
    operator has to sign out of their own workspace and back in as a second, generated
    account to configure their own machine."""
    from api.middleware import tenant as tenant_middleware
    from core.tenancy.models import Tenant
    from core.tenancy.scope import unscoped_session

    # Signup, not register: self-registration grants `viewer`, and a viewer is
    # deliberately NOT a platform admin. Only the organization's owner is -- which the
    # first signup on a fresh deployment makes you.
    org = f"Solo {uuid.uuid4().hex[:6]}"
    signup = client.post(
        "/auth/signup",
        json={
            "organization": org,
            "email": "owner@example.com",
            "password": "correct horse",
            "display_name": "Owner",
        },
    )
    assert signup.status_code == 200, signup.text
    token = signup.json()["access_token"]
    async with unscoped_session() as session:
        sole = await session.scalar(
            select(Tenant).where(Tenant.slug == signup.json()["tenant_slug"])
        )
        session.expunge(sole)
    monkeypatch.setattr(tenant_middleware, "_sole_tenant", lambda: _async(sole))

    me = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    assert me.json()["platform_admin"] is True
    # ...but their home is still their own workspace, not the admin console.
    assert me.json()["admin_tenant"] is False


async def test_a_tenant_owner_is_not_a_platform_admin_in_multi_tenant_mode(
    client: TestClient, db_available: None
) -> None:
    """The boundary that matters: on a box hosting several organizations, owning one
    grants nothing over the others or over the deployment."""
    signup = client.post(
        "/auth/signup",
        json={
            "organization": f"Org {uuid.uuid4().hex[:6]}",
            "email": "notadmin@example.com",
            "password": "correct horse",
            "display_name": "Owner",
        },
    )
    assert signup.status_code == 200, signup.text
    token = signup.json()["access_token"]

    me = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["platform_admin"] is False
    assert (
        client.get(
            "/admin/retrieval-models/cache", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 403
    )


async def test_the_single_tenant_grant_evaporates_once_a_second_org_exists(
    client: TestClient, db_available: None, single_tenant: None
) -> None:
    """Fails closed. `_sole_tenant` is unmocked here and the shared test database holds
    many tenants, so there is no sole organization -- and an owner of one of them must
    not be able to administer the deployment the others live on."""
    await _new_tenant_slug()
    signup = client.post(
        "/auth/signup",
        json={
            "organization": f"Org {uuid.uuid4().hex[:6]}",
            "email": "notsole@example.com",
            "password": "correct horse",
            "display_name": "Owner",
        },
    )
    assert signup.status_code == 200, signup.text
    token = signup.json()["access_token"]

    me = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["platform_admin"] is False
    assert (
        client.get(
            "/admin/retrieval-models/cache", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 403
    )


async def test_a_viewer_who_joins_the_solo_org_is_not_an_admin(
    client: TestClient, db_available: None, single_tenant: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "The sole organization's owner" is load-bearing, not shorthand for "anyone in it".

    A solo deployment is still a place a second person can be invited, and self-
    registration deliberately grants `viewer`. Inheriting the deployment's admin console
    from that would be a real privilege escalation."""
    from api.middleware import tenant as tenant_middleware
    from core.tenancy.models import Tenant
    from core.tenancy.scope import unscoped_session

    signup = client.post(
        "/auth/signup",
        json={
            "organization": f"Solo {uuid.uuid4().hex[:6]}",
            "email": "the-owner@example.com",
            "password": "correct horse",
            "display_name": "Owner",
        },
    )
    assert signup.status_code == 200, signup.text
    slug = signup.json()["tenant_slug"]
    async with unscoped_session() as session:
        sole = await session.scalar(select(Tenant).where(Tenant.slug == slug))
        session.expunge(sole)
    monkeypatch.setattr(tenant_middleware, "_sole_tenant", lambda: _async(sole))

    # A new organization starts closed to self-registration, so open it: this test is
    # about what a self-registered viewer may DO, not about who is let in.
    from core.tenancy.registration import set_policy

    await set_policy(sole.id, "open")
    viewer_token = _register(client, slug, "the-viewer@example.com")

    me = client.get("/me", headers={"Authorization": f"Bearer {viewer_token}"})
    assert me.json()["platform_admin"] is False
    assert (
        client.get(
            "/admin/retrieval-models/cache",
            headers={"Authorization": f"Bearer {viewer_token}"},
        ).status_code
        == 403
    )


async def test_config_reports_multi_tenant_once_several_orgs_exist(
    client: TestClient, db_available: None, single_tenant: None
) -> None:
    """The setting is intent; the number of organizations is fact.

    A near-miss worth a test: a nine-organization cluster had carried
    `SINGLE_TENANT_UI=true` in its manifests unnoticed, because nothing acted on it
    until the login form did. Reporting the setting alone would have hidden the
    organization field there -- leaving nothing to type and nothing the server could
    infer, which is not a degraded login but no login at all."""
    await _new_tenant_slug()
    await _new_tenant_slug()

    body = client.get("/auth/config").json()
    assert body["single_tenant"] is False
    assert body["has_organization"] is True
