"""Declared registries and the runtime-image policy, over real HTTP.

What has to hold: only a platform admin can declare a registry or set the allowlist; a
registry credential is write-only and never comes back; and the rules actually bite where
an organization types an image -- another tenant's image is refused, and so is anything
outside a set allowlist.
"""

from __future__ import annotations

import pathlib
import uuid
from collections.abc import Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text

from api.main import app as main_app
from api.redis_client import get_redis
from core.tenancy.scope import tenant_scope, unscoped_session
from core.tenancy.seed import seed_dev_tenant

_SECRET = "registry-pw-SENTINEL-7f3a"


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture(autouse=True)
def _store_root(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    # Creating a repo creates its hosted store; keep that out of /app.
    monkeypatch.setenv("PYRRHULA_MCP_GIT_ROOT", str(tmp_path / "repos"))


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(main_app) as c:
        yield c


async def _owner(client: TestClient, slug: str) -> dict[str, str]:
    email = f"u-{uuid.uuid4().hex[:8]}@example.com"
    resp = client.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery", "display_name": "U"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert resp.status_code == 200, resp.text
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
    return {
        "Authorization": f"Bearer {resp.json()['access_token']}",
        "X-Pyrrhula-Tenant": slug,
    }


def _key() -> str:
    return f"reg-{uuid.uuid4().hex[:8]}"


def test_only_a_platform_admin_may_declare_a_registry(
    client: TestClient, db_available: None, platform_admin_headers: dict[str, str]
) -> None:
    body = {"key": _key(), "pull_host": "localhost:5000"}
    assert client.post("/admin/image-registries", json=body).status_code == 401
    created = client.post("/admin/image-registries", json=body, headers=platform_admin_headers)
    assert created.status_code == 201, created.text
    client.delete(f"/admin/image-registries/{body['key']}", headers=platform_admin_headers)


async def test_an_organization_owner_is_not_a_platform_admin(
    client: TestClient, db_available: None
) -> None:
    slug = f"img-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    # A second tenant, so "the sole organization's owner is the platform admin" no longer
    # applies -- that single-tenant rule is legitimate and not what this test is about.
    await seed_dev_tenant(slug=f"img2-{uuid.uuid4().hex[:8]}")
    owner = await _owner(client, slug)
    resp = client.post(
        "/admin/image-registries", json={"key": _key(), "pull_host": "x.io"}, headers=owner
    )
    assert resp.status_code == 403


def test_a_registry_credential_is_write_only(
    client: TestClient, db_available: None, platform_admin_headers: dict[str, str]
) -> None:
    key = _key()
    client.post(
        "/admin/image-registries",
        json={"key": key, "pull_host": "registry.example.com"},
        headers=platform_admin_headers,
    )
    try:
        resp = client.put(
            f"/admin/image-registries/{key}/credential",
            json={"username": "reader", "password": _SECRET},
            headers=platform_admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["has_credential"] is True
        assert resp.json()["credential_username"] == "reader"
        listing = client.get("/admin/image-registries", headers=platform_admin_headers)
        for body in (resp.text, listing.text):
            assert _SECRET not in body, "a registry credential must never come back"
    finally:
        client.delete(f"/admin/image-registries/{key}", headers=platform_admin_headers)


def test_docker_hub_needs_flat_paths_and_an_acknowledgement(
    client: TestClient, db_available: None, platform_admin_headers: dict[str, str]
) -> None:
    key = _key()
    nested = client.post(
        "/admin/image-registries",
        json={"key": key, "pull_host": "docker.io", "path_prefix": "acme"},
        headers=platform_admin_headers,
    )
    assert nested.status_code == 422 and "flat" in nested.text
    unacked = client.post(
        "/admin/image-registries",
        json={"key": key, "pull_host": "docker.io", "path_prefix": "acme", "path_style": "flat"},
        headers=platform_admin_headers,
    )
    assert unacked.status_code == 422 and "public" in unacked.text


async def test_an_organization_cannot_type_another_organizations_image(
    client: TestClient, db_available: None, platform_admin_headers: dict[str, str]
) -> None:
    key = _key()
    client.post(
        "/admin/image-registries",
        json={"key": key, "pull_host": "registry.example.com", "path_prefix": key},
        headers=platform_admin_headers,
    )
    try:
        slug = f"img-{uuid.uuid4().hex[:8]}"
        await seed_dev_tenant(slug=slug)
        owner = await _owner(client, slug)
        someone_else = uuid.uuid4().hex
        resp = client.post(
            "/repos",
            json={
                "key": f"r{uuid.uuid4().hex[:6]}",
                "name": "R",
                "runtime": "custom",
                "runtime_image": f"registry.example.com/{key}/t{someone_else}/godot:1",
            },
            headers=owner,
        )
        assert resp.status_code == 422, resp.text
        assert "does not belong to this organization" in resp.text
        assert someone_else not in resp.text
    finally:
        client.delete(f"/admin/image-registries/{key}", headers=platform_admin_headers)


async def test_the_allowlist_bites_at_the_repo_form_and_can_be_lifted(
    client: TestClient, db_available: None, platform_admin_headers: dict[str, str]
) -> None:
    slug = f"img-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    owner = await _owner(client, slug)

    def make(image: str) -> int:
        return client.post(
            "/repos",
            json={
                "key": f"r{uuid.uuid4().hex[:6]}",
                "name": "R",
                "runtime": "custom",
                "runtime_image": image,
            },
            headers=owner,
        ).status_code

    set_ = client.put(
        "/admin/runtime-image-allowlist",
        json={"prefixes": ["ghcr.io/tuturu742/"]},
        headers=platform_admin_headers,
    )
    assert set_.status_code == 200, set_.text
    try:
        assert make("python:3.12") == 422
        assert make("ghcr.io/tuturu742/godot-node:1") == 201
    finally:
        client.put(
            "/admin/runtime-image-allowlist", json={"prefixes": []}, headers=platform_admin_headers
        )
    assert make("python:3.12") == 201, "an empty allowlist restores today's behaviour"


async def test_an_owner_imports_a_pinned_image_and_a_tag_is_refused(
    client: TestClient, db_available: None
) -> None:
    slug = f"img-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    owner = await _owner(client, slug)
    digest = "sha256:" + "a" * 64
    tagged = client.post(
        "/images/import", json={"name": "godot-node", "image": "ghcr.io/x/godot:4"}, headers=owner
    )
    assert tagged.status_code == 422 and "digest" in tagged.text
    pinned = client.post(
        "/images/import",
        json={"name": "godot-node", "image": f"ghcr.io/x/godot@{digest}"},
        headers=owner,
    )
    assert pinned.status_code == 202, pinned.text
    assert pinned.json()["status"] == "verifying"
    listing = client.get("/images", headers=owner).json()
    assert [i["name"] for i in listing] == ["godot-node"]
    # Not a runtime until the worker's check passes.
    runtimes = client.get("/repos/runtimes", headers=owner).json()
    assert "godot-node" not in {r["key"] for r in runtimes}
