"""Phase B admin console: token gate, tenant/user creation, and that deactivation actually
blocks the main app -- all over real HTTP against both apps sharing one database."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.admin.app import app as admin_app
from api.main import app as main_app
from api.redis_client import get_redis
from core.config import get_settings

_ADMIN_TOKEN = "test-admin-token"


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture(autouse=True)
def _admin_token() -> Iterator[None]:
    settings = get_settings()
    original = settings.admin_token
    settings.admin_token = _ADMIN_TOKEN
    yield
    settings.admin_token = original


@pytest.fixture
def admin() -> Iterator[TestClient]:
    with TestClient(admin_app) as c:
        yield c


@pytest.fixture
def api() -> Iterator[TestClient]:
    with TestClient(main_app) as c:
        yield c


def _auth(token: str = _ADMIN_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_admin_requires_token(admin: TestClient, db_available: None) -> None:
    assert admin.get("/admin/tenants").status_code == 401
    assert admin.get("/admin/tenants", headers=_auth("nope")).status_code == 401
    assert admin.get("/admin/tenants", headers=_auth()).status_code == 200


def test_create_tenant_and_user_then_login(
    admin: TestClient, api: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"adm-{uuid.uuid4().hex[:8]}"
    email = f"{uuid.uuid4().hex}@example.com"
    resp = admin.post(
        "/admin/tenants",
        json={
            "slug": slug,
            "name": "Acme",
            "owner_email": email,
            "owner_password": "hunter2hunter",
        },
        headers=_auth(),
    )
    assert resp.status_code == 201, resp.text

    # it appears in the listing
    tenants = admin.get("/admin/tenants", headers=_auth()).json()
    assert any(t["slug"] == slug and t["member_count"] >= 1 for t in tenants)

    # the owner can log into the main app and reach a protected route
    login = api.post(
        "/auth/login",
        json={"email": email, "password": "hunter2hunter"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    assert api.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_two_tenants_can_share_an_owner_email(
    admin: TestClient, api: TestClient, db_available: None, redis_available: None
) -> None:
    """Tenants are independent, so provisioning one must not be blocked by a row in
    another. This used to 409 with "email already registered" because identity
    uniqueness was global -- an operator could not create a tenant for a person who
    already owned one, and the blocking row lived in a tenant they cannot see."""
    email = f"{uuid.uuid4().hex}@example.com"
    slugs = [f"share-{uuid.uuid4().hex[:8]}", f"share-{uuid.uuid4().hex[:8]}"]

    for slug in slugs:
        resp = admin.post(
            "/admin/tenants",
            json={
                "slug": slug,
                "name": slug,
                "owner_email": email,
                "owner_password": "hunter2hunter",
            },
            headers=_auth(),
        )
        assert resp.status_code == 201, resp.text

    # Both accounts are real and separate: each logs into its own tenant, and the
    # sessions are for different principals.
    principals = set()
    for slug in slugs:
        login = api.post(
            "/auth/login",
            json={"email": email, "password": "hunter2hunter"},
            headers={"X-Pyrrhula-Tenant": slug},
        )
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]
        me = api.get("/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200, me.text
        principals.add(me.json()["principal_id"])
    assert len(principals) == 2


def test_duplicate_owner_email_in_one_tenant_is_refused(
    admin: TestClient, db_available: None, redis_available: None
) -> None:
    """Per-tenant, not absent -- a second login with the same email inside one tenant
    would be ambiguous at verify time."""
    slug = f"dup-{uuid.uuid4().hex[:8]}"
    email = f"{uuid.uuid4().hex}@example.com"
    assert (
        admin.post(
            "/admin/tenants",
            json={
                "slug": slug,
                "name": "T",
                "owner_email": email,
                "owner_password": "hunter2hunter",
            },
            headers=_auth(),
        ).status_code
        == 201
    )

    tenant_id = next(
        t["id"] for t in admin.get("/admin/tenants", headers=_auth()).json() if t["slug"] == slug
    )
    dup = admin.post(
        f"/admin/tenants/{tenant_id}/users",
        json={
            "email": email,
            "password": "hunter2hunter",
            "display_name": "Impostor",
            "role": "viewer",
        },
        headers=_auth(),
    )
    assert dup.status_code == 409, dup.text


def test_deactivate_tenant_blocks_login(
    admin: TestClient, api: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"adm-{uuid.uuid4().hex[:8]}"
    email = f"{uuid.uuid4().hex}@example.com"
    tid = admin.post(
        "/admin/tenants",
        json={"slug": slug, "name": "T", "owner_email": email, "owner_password": "hunter2hunter"},
        headers=_auth(),
    ).json()["tenant_id"]

    token = api.post(
        "/auth/login",
        json={"email": email, "password": "hunter2hunter"},
        headers={"X-Pyrrhula-Tenant": slug},
    ).json()["access_token"]

    assert admin.post(f"/admin/tenants/{tid}/deactivate", headers=_auth()).status_code == 200
    # existing token is rejected, and a fresh login is refused
    assert api.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 403
    assert (
        api.post(
            "/auth/login",
            json={"email": email, "password": "hunter2hunter"},
            headers={"X-Pyrrhula-Tenant": slug},
        ).status_code
        == 403
    )
    # reactivating restores access
    assert admin.post(f"/admin/tenants/{tid}/reactivate", headers=_auth()).status_code == 200
    assert (
        api.post(
            "/auth/login",
            json={"email": email, "password": "hunter2hunter"},
            headers={"X-Pyrrhula-Tenant": slug},
        ).status_code
        == 200
    )


def test_deactivate_user_blocks_requests(
    admin: TestClient, api: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"adm-{uuid.uuid4().hex[:8]}"
    owner_email = f"{uuid.uuid4().hex}@example.com"
    tid = admin.post(
        "/admin/tenants",
        json={
            "slug": slug,
            "name": "T",
            "owner_email": owner_email,
            "owner_password": "hunter2hunter",
        },
        headers=_auth(),
    ).json()["tenant_id"]

    member_email = f"{uuid.uuid4().hex}@example.com"
    pid = admin.post(
        f"/admin/tenants/{tid}/users",
        json={
            "email": member_email,
            "password": "hunter2hunter",
            "display_name": "Mem",
            "role": "editor",
        },
        headers=_auth(),
    ).json()["principal_id"]

    token = api.post(
        "/auth/login",
        json={"email": member_email, "password": "hunter2hunter"},
        headers={"X-Pyrrhula-Tenant": slug},
    ).json()["access_token"]
    assert api.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    assert (
        admin.post(f"/admin/tenants/{tid}/users/{pid}/deactivate", headers=_auth()).status_code
        == 200
    )
    assert api.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 403

    # duplicate email -> 409
    dup = admin.post(
        f"/admin/tenants/{tid}/users",
        json={
            "email": member_email,
            "password": "hunter2hunter",
            "display_name": "Dup",
            "role": "viewer",
        },
        headers=_auth(),
    )
    assert dup.status_code == 409, dup.text


def test_pin_tenant_overlay(admin: TestClient, db_available: None, redis_available: None) -> None:
    slug = f"adm-{uuid.uuid4().hex[:8]}"
    tid = admin.post("/admin/tenants", json={"slug": slug, "name": "O"}, headers=_auth()).json()[
        "tenant_id"
    ]

    overlays = admin.get(f"/admin/tenants/{tid}/overlays", headers=_auth()).json()
    keys = {o["key"] for o in overlays}
    assert {"rpg_v1", "swdev_v1"} <= keys  # shipped system overlays are offered

    # unset by default
    before = next(t for t in admin.get("/admin/tenants", headers=_auth()).json() if t["id"] == tid)
    assert before["overlay_key"] is None

    assert (
        admin.post(
            f"/admin/tenants/{tid}/overlay", json={"overlay_key": "swdev_v1"}, headers=_auth()
        ).status_code
        == 200
    )
    after = next(t for t in admin.get("/admin/tenants", headers=_auth()).json() if t["id"] == tid)
    assert after["overlay_key"] == "swdev_v1"


def test_set_workflow_pins_overlay_and_provisions_git(
    admin: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"adm-{uuid.uuid4().hex[:8]}"
    tid = admin.post("/admin/tenants", json={"slug": slug, "name": "W"}, headers=_auth()).json()[
        "tenant_id"
    ]

    workflows = {w["key"]: w for w in admin.get("/admin/workflows", headers=_auth()).json()}
    assert {"rpg", "swdev", "default"} <= set(workflows)
    assert workflows["swdev"]["persona_type_labels"]["supervisor"] == "Lead"
    assert workflows["swdev"]["capabilities"]["mcp_servers"][0]["key"] == "git"
    rpg_servers = workflows["rpg"]["capabilities"]["mcp_servers"]
    assert [s["key"] for s in rpg_servers] == ["resolution"]

    # swdev -> overlay pinned + git provisioned on the tenant's workspace
    resp = admin.post(
        f"/admin/tenants/{tid}/workflow", json={"workflow_key": "swdev"}, headers=_auth()
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["capabilities_applied"] == ["git"]
    row = next(t for t in admin.get("/admin/tenants", headers=_auth()).json() if t["id"] == tid)
    assert row["workflow_key"] == "swdev" and row["overlay_key"] == "swdev_v1"

    # rpg -> overlay switches, no capabilities to add
    resp = admin.post(
        f"/admin/tenants/{tid}/workflow", json={"workflow_key": "rpg"}, headers=_auth()
    )
    assert resp.status_code == 200 and resp.json()["capabilities_applied"] == ["resolution"]
    row = next(t for t in admin.get("/admin/tenants", headers=_auth()).json() if t["id"] == tid)
    assert row["workflow_key"] == "rpg" and row["overlay_key"] == "rpg_v1"


# ── admin-tenant JWT path (admin lives in the main app now) ──────────────────────────
async def test_admin_tenant_jwt_reaches_admin_routes_on_main_app(
    api: TestClient, db_available: None, redis_available: None
) -> None:
    """Logging in with organization ``admin`` yields a JWT that require_platform_admin
    accepts on the MAIN app -- and /me flags the account so the web shell can swap nav."""
    from adapters.identity.local.argon2_provider import LocalArgon2IdentityProvider
    from core.tenancy.admin import ADMIN_TENANT_ID
    from core.tenancy.provisioning import create_tenant_user

    email = f"{uuid.uuid4().hex}@example.com"
    principal_id = await create_tenant_user(ADMIN_TENANT_ID, "Platform Admin", "owner")
    await LocalArgon2IdentityProvider().register_local(
        ADMIN_TENANT_ID, principal_id, email, "correct horse battery"
    )

    login = api.post(
        "/auth/login",
        json={"email": email, "password": "correct horse battery"},
        headers={"X-Pyrrhula-Tenant": "admin"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]

    assert api.get("/admin/tenants", headers=_auth(token)).status_code == 200

    me = api.get("/me", headers=_auth(token)).json()
    assert me["platform_admin"] is True and me["tenant_slug"] == "admin"


async def test_ordinary_tenant_jwt_is_rejected_by_admin_routes(
    api: TestClient, db_available: None, redis_available: None
) -> None:
    from core.tenancy.seed import seed_dev_tenant

    slug = f"adm-jwt-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    email = f"{uuid.uuid4().hex}@example.com"
    register = api.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery", "display_name": "T"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert register.status_code == 200, register.text
    token = register.json()["access_token"]

    resp = api.get("/admin/tenants", headers=_auth(token))
    assert resp.status_code == 403

    me = api.get("/me", headers=_auth(token)).json()
    assert me["platform_admin"] is False


# ── tenant MCP capability grants (M-B) ───────────────────────────────────────────────
async def test_tenant_mcp_grant_materializes_and_survives_workflow_reapply(
    admin: TestClient, db_available: None, redis_available: None
) -> None:
    from core.mcp.registry import list_servers
    from core.tenancy.provisioning import list_tenant_workspace_ids

    slug = f"adm-mcp-{uuid.uuid4().hex[:8]}"
    tid = admin.post("/admin/tenants", json={"slug": slug, "name": "M"}, headers=_auth()).json()[
        "tenant_id"
    ]

    put = admin.put(
        f"/admin/tenants/{tid}/mcp-servers",
        json={
            "key": "engine",
            "url": "http://godot-mcp:8090",
            "enabled_tools": ["run_tests", "export_build"],
            "effectful_tools": ["export_build"],
        },
        headers=_auth(),
    )
    assert put.status_code == 200, put.text
    assert put.json()["workspaces_applied"] >= 1

    tenant_uuid = uuid.UUID(tid)
    wid = (await list_tenant_workspace_ids(tenant_uuid))[0]
    rows = {r.key: r for r in await list_servers(tenant_uuid, wid)}
    assert "engine" in rows and rows["engine"].url == "http://godot-mcp:8090"
    assert rows["engine"].effectful_tools == ["export_build"]

    # A workflow (re-)apply keeps the grant: pack servers first, grants after.
    resp = admin.post(
        f"/admin/tenants/{tid}/workflow", json={"workflow_key": "swdev"}, headers=_auth()
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["capabilities_applied"]) == {"git", "engine"}
    rows = {r.key: r for r in await list_servers(tenant_uuid, wid)}
    assert "engine" in rows and "git" in rows

    listed = admin.get(f"/admin/tenants/{tid}/mcp-servers", headers=_auth()).json()
    assert [g["key"] for g in listed] == ["engine"]

    # Delete removes the grant AND its workspace materializations.
    assert (
        admin.delete(f"/admin/tenants/{tid}/mcp-servers/engine", headers=_auth()).status_code == 204
    )
    rows = {r.key: r for r in await list_servers(tenant_uuid, wid)}
    assert "engine" not in rows and "git" in rows


def test_workspace_mcp_put_requires_workflow_manage(
    admin: TestClient, api: TestClient, db_available: None, redis_available: None
) -> None:
    """A self-registered viewer must NOT be able to widen a workspace's allowlist."""
    slug = f"adm-mcpz-{uuid.uuid4().hex[:8]}"
    body = admin.post(
        "/admin/tenants",
        json={
            "slug": slug,
            "name": "Z",
            "owner_email": f"{uuid.uuid4().hex}@example.com",
            "owner_password": "hunter2hunter",
        },
        headers=_auth(),
    ).json()
    wid = body["workspace_id"]

    viewer_email = f"{uuid.uuid4().hex}@example.com"
    viewer_token = api.post(
        "/auth/register",
        json={"email": viewer_email, "password": "hunter2hunter", "display_name": "V"},
        headers={"X-Pyrrhula-Tenant": slug},
    ).json()["access_token"]

    resp = api.put(
        "/mcp-servers",
        json={
            "workspace_id": wid,
            "key": "sneaky",
            "url": "http://evil.example/mcp",
            "enabled_tools": ["anything"],
        },
        headers=_auth(viewer_token),
    )
    assert resp.status_code == 403, resp.text
