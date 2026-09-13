"""A1.1: knowledge authoring over real HTTP — create a source, add entries, publish, and
attach the same source to two workspaces with different settings."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture
def client():  # noqa: ANN201
    with TestClient(app) as test_client:
        yield test_client


def _register_and_login(client: TestClient, slug: str) -> str:
    email = f"{uuid.uuid4().hex}@example.com"
    response = client.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery", "display_name": "Tester"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


async def test_create_add_entry_publish_over_http(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"kn-http-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = client.post(
        "/knowledge/sources",
        json={"key": "core-rules", "name": "Core Rules", "class": "rules"},
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    source_id = create_resp.json()["id"]
    assert create_resp.json()["current_version_id"] is None

    entry_resp = client.put(
        f"/knowledge/sources/{source_id}/entries/grappling",
        json={
            "title": "Grappling",
            "body_md": "Roll 1d20+STR.",
            "class": "rules",
            "scope_key": "workspace_public",
        },
        headers=headers,
    )
    assert entry_resp.status_code == 200, entry_resp.text

    draft_resp = client.get(f"/knowledge/sources/{source_id}/entries", headers=headers)
    assert {e["entry_key"] for e in draft_resp.json()} == {"grappling"}

    publish_resp = client.post(f"/knowledge/sources/{source_id}/publish", json={}, headers=headers)
    assert publish_resp.status_code == 201, publish_resp.text
    version = publish_resp.json()
    assert version["version_number"] == 1

    published_resp = client.get(
        f"/knowledge/sources/{source_id}/versions/{version['id']}/entries", headers=headers
    )
    assert {e["entry_key"] for e in published_resp.json()} == {"grappling"}


async def test_own_source_is_not_library_and_entry_activation_fields_round_trip(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    """D1.1: SourceResponse.is_library and EntryResponse's activation fields are new --
    both must actually round-trip what was written, not just accept it on write."""
    slug = f"kn-activation-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = client.post(
        "/knowledge/sources",
        json={"key": "core-rules", "name": "Core Rules", "class": "rules"},
        headers=headers,
    )
    assert create_resp.json()["is_library"] is False
    source_id = create_resp.json()["id"]

    entry_resp = client.put(
        f"/knowledge/sources/{source_id}/entries/grappling",
        json={
            "title": "Grappling",
            "body_md": "Roll 1d20+STR.",
            "class": "rules",
            "scope_key": "workspace_public",
            "keys": ["grapple", "wrestle"],
            "secondary_keys": ["strength"],
            "logic": "OR",
            "use_regex": False,
            "constant": True,
            "sticky": 3,
            "cooldown": 2,
            "delay": 1,
            "trigger_pct": 50,
            "inclusion_group": "combat-moves",
            "position": "after_char",
        },
        headers=headers,
    )
    assert entry_resp.status_code == 200, entry_resp.text

    draft_resp = client.get(f"/knowledge/sources/{source_id}/entries", headers=headers)
    entry = draft_resp.json()[0]
    assert entry["keys"] == ["grapple", "wrestle"]
    assert entry["secondary_keys"] == ["strength"]
    assert entry["logic"] == "OR"
    assert entry["constant"] is True
    assert entry["sticky"] == 3
    assert entry["cooldown"] == 2
    assert entry["delay"] == 1
    assert entry["trigger_pct"] == 50
    assert entry["inclusion_group"] == "combat-moves"
    assert entry["position"] == "after_char"


async def test_attach_same_source_to_two_workspaces_over_http(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"kn-attach-http-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_a = await seed_dev_tenant(slug=slug)
    async with tenant_scope(tenant_id) as session:
        ws_b = Workspace(tenant_id=tenant_id, key="second", name="Second Workspace")
        session.add(ws_b)
        await session.flush()
        workspace_b = ws_b.id

    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = client.post(
        "/knowledge/sources",
        json={"key": "core-rules", "name": "Core Rules", "class": "rules"},
        headers=headers,
    )
    source_id = create_resp.json()["id"]

    attach_a = client.post(
        f"/knowledge/sources/{source_id}/attachments",
        json={
            "workspace_id": str(workspace_a),
            "scope_key": "workspace_public",
            "priority_weight": 0.75,
        },
        headers=headers,
    )
    assert attach_a.status_code == 201, attach_a.text
    attach_b = client.post(
        f"/knowledge/sources/{source_id}/attachments",
        json={
            "workspace_id": str(workspace_b),
            "scope_key": "faction_thieves",
            "priority_weight": 0.25,
        },
        headers=headers,
    )
    assert attach_b.status_code == 201, attach_b.text

    list_a = client.get(f"/knowledge/workspaces/{workspace_a}/attachments", headers=headers)
    list_b = client.get(f"/knowledge/workspaces/{workspace_b}/attachments", headers=headers)
    assert list_a.json()[0]["scope_key"] == "workspace_public"
    assert list_a.json()[0]["priority_weight"] == 0.75
    assert list_b.json()[0]["scope_key"] == "faction_thieves"
    assert list_b.json()[0]["priority_weight"] == 0.25

    # D1.1: the source detail page's own read direction -- which workspaces is *this
    # source* attached to (the other query direction from list_a/list_b above).
    by_source = client.get(f"/knowledge/sources/{source_id}/attachments", headers=headers)
    assert by_source.status_code == 200, by_source.text
    assert {a["workspace_id"] for a in by_source.json()} == {str(workspace_a), str(workspace_b)}


async def test_knowledge_endpoints_require_auth(client: TestClient, db_available: None) -> None:
    response = client.get("/knowledge/sources")
    assert response.status_code == 401
