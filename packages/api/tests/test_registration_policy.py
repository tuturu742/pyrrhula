"""Who may join an existing organization.

`POST /auth/register` names its tenant in the `X-Pyrrhula-Tenant` header and used to
create a member of it with no gate but the per-IP rate limiter. Anyone who could reach
the API could obtain a viewer membership -- and a session token -- inside any
organization on the deployment. Confirmed against a running deployment before this was
written: an unauthenticated POST naming another tenant returned 200 and a usable token.

Its sibling `/auth/signup` has always checked `allow_tenant_signup`, and cannot reach an
existing organization anyway (a slug collision allocates a new suffix rather than
joining one). The ungated endpoint was the dangerous one.
"""

import uuid
from collections.abc import Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
from core.tenancy.registration import get_policy, list_pending, set_policy
from core.tenancy.seed import seed_dev_tenant


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    # TestClient presents as one fixed synthetic client, so every test here shares a
    # rate-limit key against a persistent Redis. Reset per test or a repeated run starts
    # returning 429 instead of the status being asserted.
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


async def _tenant(policy: str) -> tuple[uuid.UUID, str]:
    slug = f"regpol-{uuid.uuid4().hex[:8]}"
    tenant_id, _workspace_id, _owner = await seed_dev_tenant(slug=slug)
    await set_policy(tenant_id, policy)
    return tenant_id, slug


def _register(client: TestClient, slug: str, email: str) -> object:
    return client.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery", "display_name": "Outsider"},
        headers={"X-Pyrrhula-Tenant": slug},
    )


async def test_a_closed_organization_refuses_a_stranger(
    client: TestClient, db_available: None
) -> None:
    """The regression. A 403 and, crucially, no token."""
    _tid, slug = await _tenant("closed")
    response = _register(client, slug, f"{uuid.uuid4().hex}@example.com")
    assert response.status_code == 403, response.text
    assert "access_token" not in response.json()


async def test_closed_is_what_an_organization_that_has_not_chosen_gets(
    client: TestClient, db_available: None
) -> None:
    """A deployment that upgrades into this must not keep the hole because nobody has
    visited the setting yet.

    Built the production way -- `create_tenant`, which is what signup and the admin
    console use -- so this asserts the default a REAL organization gets, not one the dev
    seeder arranged. (`seed_dev_tenant` opens its tenants on purpose: most tests obtain a
    token by registering.)"""
    from core.tenancy.provisioning import create_tenant

    slug = f"regpol-{uuid.uuid4().hex[:8]}"
    tenant_id, _workspace_id = await create_tenant(f"Reg {slug}", slug)
    assert await get_policy(tenant_id) == "closed"
    assert _register(client, slug, f"{uuid.uuid4().hex}@example.com").status_code == 403


async def test_an_open_organization_still_lets_people_in(
    client: TestClient, db_available: None
) -> None:
    """The setting is a choice, not a ban: `open` is the old behaviour, now deliberate."""
    _tid, slug = await _tenant("open")
    response = _register(client, slug, f"{uuid.uuid4().hex}@example.com")
    assert response.status_code == 200, response.text
    assert response.json()["access_token"]


async def test_request_mode_queues_the_application_and_issues_nothing(
    client: TestClient, db_available: None
) -> None:
    """202, no token, no membership -- an application, not an account."""
    tenant_id, slug = await _tenant("request")
    email = f"{uuid.uuid4().hex}@example.com"
    response = _register(client, slug, email)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert "access_token" not in body

    pending = await list_pending(tenant_id)
    assert [p.email for p in pending] == [email]

    # And it is not a login: the applicant cannot sign in while waiting.
    login = client.post(
        "/auth/login",
        json={"email": email, "password": "correct horse battery"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert login.status_code != 200


async def test_applying_twice_does_not_fill_the_queue(
    client: TestClient, db_available: None
) -> None:
    tenant_id, slug = await _tenant("request")
    email = f"{uuid.uuid4().hex}@example.com"
    assert _register(client, slug, email).status_code == 202
    assert _register(client, slug, email).status_code == 409
    assert len(await list_pending(tenant_id)) == 1


async def test_a_typo_in_the_stored_policy_does_not_open_the_door(
    client: TestClient, db_available: None
) -> None:
    """An unrecognised value -- a typo, a hand-edited row, a value from a newer version
    -- must fail towards closed. Failing towards open would put an organization on the
    internet because someone misspelled a word."""
    from core.tenancy.models import Tenant
    from core.tenancy.scope import unscoped_session

    tenant_id, slug = await _tenant("open")
    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.settings = {**dict(tenant.settings or {}), "registration_policy": "opne"}

    assert await get_policy(tenant_id) == "closed"
    assert _register(client, slug, f"{uuid.uuid4().hex}@example.com").status_code == 403


async def test_one_organizations_policy_does_not_travel_to_another(
    client: TestClient, db_available: None
) -> None:
    """The policy is the organization's, not the deployment's."""
    _open_id, open_slug = await _tenant("open")
    _shut_id, shut_slug = await _tenant("closed")

    assert _register(client, open_slug, f"{uuid.uuid4().hex}@example.com").status_code == 200
    assert _register(client, shut_slug, f"{uuid.uuid4().hex}@example.com").status_code == 403


_ADMIN_TOKEN = "registration-policy-test-token"


@pytest.fixture(autouse=True)
def _admin_token() -> Iterator[None]:
    from core.config import get_settings

    settings = get_settings()
    original = settings.admin_token
    settings.admin_token = _ADMIN_TOKEN
    yield
    settings.admin_token = original


def _admin(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


async def test_approving_an_application_makes_a_working_account(
    client: TestClient, db_available: None
) -> None:
    """The point of `request` mode: the password the applicant chose still works, so
    approval does not mean asking them to pick one again."""
    tenant_id, slug = await _tenant("request")
    email = f"{uuid.uuid4().hex}@example.com"
    assert _register(client, slug, email).status_code == 202

    listed = client.get(f"/admin/tenants/{tenant_id}/registration-requests", headers=_admin(client))
    assert listed.status_code == 200, listed.text
    assert [r["email"] for r in listed.json()] == [email]
    request_id = listed.json()[0]["id"]

    approved = client.post(
        f"/admin/tenants/{tenant_id}/registration-requests/{request_id}/approve",
        headers=_admin(client),
    )
    assert approved.status_code == 200, approved.text

    login = client.post(
        "/auth/login",
        json={"email": email, "password": "correct horse battery"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert login.status_code == 200, login.text
    assert login.json()["access_token"]
    assert await list_pending(tenant_id) == []


async def test_approving_the_same_application_twice_makes_one_account(
    client: TestClient, db_available: None
) -> None:
    """Two admins clicking at once must not both mint an account: the row is claimed and
    marked decided in one statement, so the second attempt finds nothing pending."""
    tenant_id, slug = await _tenant("request")
    assert _register(client, slug, f"{uuid.uuid4().hex}@example.com").status_code == 202
    request_id = client.get(
        f"/admin/tenants/{tenant_id}/registration-requests", headers=_admin(client)
    ).json()[0]["id"]

    first = client.post(
        f"/admin/tenants/{tenant_id}/registration-requests/{request_id}/approve",
        headers=_admin(client),
    )
    second = client.post(
        f"/admin/tenants/{tenant_id}/registration-requests/{request_id}/approve",
        headers=_admin(client),
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 404, second.text


async def test_rejecting_drops_the_password_it_was_holding(
    client: TestClient, db_available: None
) -> None:
    """A refused application has no use for the applicant's credential, and keeping it
    would leave a stranger's hash in the table indefinitely."""
    from sqlalchemy import select

    from core.tenancy.registration import RegistrationRequestRow
    from core.tenancy.scope import tenant_scope

    tenant_id, slug = await _tenant("request")
    email = f"{uuid.uuid4().hex}@example.com"
    assert _register(client, slug, email).status_code == 202
    request_id = client.get(
        f"/admin/tenants/{tenant_id}/registration-requests", headers=_admin(client)
    ).json()[0]["id"]

    rejected = client.post(
        f"/admin/tenants/{tenant_id}/registration-requests/{request_id}/reject",
        json={"note": "not expected"},
        headers=_admin(client),
    )
    assert rejected.status_code == 200, rejected.text
    assert await list_pending(tenant_id) == []

    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(RegistrationRequestRow).where(RegistrationRequestRow.id == uuid.UUID(request_id))
        )
        assert row.status == "rejected"
        assert row.password_hash == "", "the refused applicant's hash was kept"

    login = client.post(
        "/auth/login",
        json={"email": email, "password": "correct horse battery"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert login.status_code != 200
