"""knowledge authoring over real HTTP — create a source, add entries, publish, and
attach the same source to two workspaces with different settings."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
from api.tests.grants import grant_tenant_role, grant_workspace_role
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
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    await grant_tenant_role(client, token, tenant_id, "editor")
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
    """SourceResponse.is_library and EntryResponse's activation fields are new --
    both must actually round-trip what was written, not just accept it on write."""
    slug = f"kn-activation-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    await grant_tenant_role(client, token, tenant_id, "editor")
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
    await grant_tenant_role(client, token, tenant_id, "editor")
    await grant_workspace_role(client, token, tenant_id, workspace_a)
    await grant_workspace_role(client, token, tenant_id, workspace_b)
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

    # the source detail page's own read direction -- which workspaces is *this
    # source* attached to (the other query direction from list_a/list_b above).
    by_source = client.get(f"/knowledge/sources/{source_id}/attachments", headers=headers)
    assert by_source.status_code == 200, by_source.text
    assert {a["workspace_id"] for a in by_source.json()} == {str(workspace_a), str(workspace_b)}


async def test_knowledge_endpoints_require_auth(client: TestClient, db_available: None) -> None:
    response = client.get("/knowledge/sources")
    assert response.status_code == 401


async def test_saturation_endpoint_reports_which_always_on_entries_fit(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    """The warning this endpoint exists for. Always-on entries take a share of a class's
    slice before search gets any of it; more of them than the share holds means some are
    absent from every turn, and nothing else in the product says which."""
    from core.process.dsl.fixtures import MINIMAL_MVP_FLOW

    slug = f"kn-sat-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    await grant_tenant_role(client, token, tenant_id, "editor")
    await grant_workspace_role(client, token, tenant_id, workspace_id)
    headers = {"Authorization": f"Bearer {token}"}

    source_id = client.post(
        "/knowledge/sources",
        json={"key": "house-rules", "name": "House Rules", "class": "rules"},
        headers=headers,
    ).json()["id"]
    # MINIMAL_MVP_FLOW gives `resolve` all 2000 of its tokens to rules. Six entries of
    # ~350 tokens each is more than the share holds. They are created in one order and
    # prioritised in the opposite one, so the report has to use the author's field rather
    # than anything incidental.
    body = " ".join(f"word{i}" for i in range(200))
    for index in range(6):
        client.put(
            f"/knowledge/sources/{source_id}/entries/always-{index}",
            json={
                "title": f"Always {index}",
                "body_md": body,
                "class": "rules",
                "scope_key": "workspace_public",
                "constant": True,
                "insertion_order": 5 - index,
            },
            headers=headers,
        )
    version_id = client.post(
        f"/knowledge/sources/{source_id}/publish", json={}, headers=headers
    ).json()["id"]
    client.post(
        f"/knowledge/sources/{source_id}/attachments",
        json={
            "workspace_id": str(workspace_id),
            "scope_key": "workspace_public",
            "version_pin": version_id,
        },
        headers=headers,
    )

    flow_id = client.post(
        "/process-definitions",
        json={"key": "mvp", "name": "MVP", "definition": MINIMAL_MVP_FLOW},
        headers=headers,
    ).json()["id"]

    resp = client.get(
        f"/knowledge/workspaces/{workspace_id}/saturation",
        params={"process_definition_id": flow_id},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    phases = {p["phase_key"]: p for p in resp.json()["phases"]}

    # A phase with no budget retrieves nothing, so it cannot be saturated and is absent.
    assert "player_act" not in phases

    rules = next(c for c in phases["resolve"]["classes"] if c["class"] == "rules")
    assert rules["constant_entries"] == 6
    assert rules["constant_tokens"] > rules["bucket_tokens"]
    # Some fit, some do not -- and the share kept room that search can still spend, which
    # is the whole point of having one.
    assert 0 < rules["admitted_constant_entries"] < 6
    assert rules["dropped_constant_entries"] == 6 - rules["admitted_constant_entries"]
    assert rules["retrievable_tokens"] > 0
    assert rules["saturated"] is False
    # Offered in the author's order, which here is the reverse of the creation order.
    assert rules["constant_entry_keys"] == [f"always-{i}" for i in range(5, -1, -1)]

    # Only a `rules` load, so the lore share in the other phase is untouched.
    narrate = {c["class"]: c for c in phases["arbiter_narrate"]["classes"]}
    assert narrate["lore"]["constant_tokens"] == 0
    assert narrate["lore"]["dropped_constant_entries"] == 0
    assert narrate["rules"]["constant_tokens"] > 0


async def test_saturation_endpoint_404s_for_an_unknown_flow(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"kn-sat404-{uuid.uuid4().hex[:8]}"
    _tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    resp = client.get(
        f"/knowledge/workspaces/{workspace_id}/saturation",
        params={"process_definition_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_knowledge_authoring_takes_the_tenant_seat_and_attaching_the_workspace_seat(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    """A self-registered viewer may read the tenant's knowledge but not author it
    (``knowledge:author``: owner, admin, editor); an editor may author it but may not
    attach it to a workspace they hold no seat in (``manage_knowledge``)."""
    slug = f"kn-authz-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    viewer = {"Authorization": f"Bearer {_register_and_login(client, slug)}"}
    editor_token = _register_and_login(client, slug)
    await grant_tenant_role(client, editor_token, tenant_id, "editor")
    editor = {"Authorization": f"Bearer {editor_token}"}

    source = {"key": "core-rules", "name": "Core Rules", "class": "rules"}
    assert client.post("/knowledge/sources", json=source, headers=viewer).status_code == 403
    created = client.post("/knowledge/sources", json=source, headers=editor)
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]

    entry = {
        "title": "Grappling",
        "body_md": "Roll.",
        "class": "rules",
        "scope_key": "workspace_public",
    }
    entry_url = f"/knowledge/sources/{source_id}/entries/grappling"
    assert client.put(entry_url, json=entry, headers=viewer).status_code == 403
    assert (
        client.post(f"/knowledge/sources/{source_id}/publish", json={}, headers=viewer).status_code
        == 403
    )
    assert (
        client.post(
            f"{entry_url}/apply-edit", json={"proposed_body_md": "x"}, headers=viewer
        ).status_code
        == 403
    )
    fork = {"from_version_id": str(uuid.uuid4()), "new_key": "fork", "new_name": "Fork"}
    assert (
        client.post(f"/knowledge/sources/{source_id}/fork", json=fork, headers=viewer).status_code
        == 403
    )

    attach = {"workspace_id": str(workspace_id), "scope_key": "workspace_public"}
    attached = client.post(
        f"/knowledge/sources/{source_id}/attachments", json=attach, headers=editor
    )
    assert attached.status_code == 403, attached.text
