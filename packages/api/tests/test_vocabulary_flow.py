"""vocabulary overlay listing/resolution/switching over real HTTP -- the fallback
chain (workspace override -> tenant default -> system default) and live relabelling.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
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


async def test_list_overlays_includes_the_three_shipped_system_overlays(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    """`swdev_v1` is the third shipped system overlay (a fresh INSERT,
    its own base migration never pre-seeded it) -- this test's own name and
    assertion set grew from two to three for exactly that reason."""
    slug = f"vocab-list-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.get("/vocabulary-overlays", headers=headers)
    assert resp.status_code == 200, resp.text
    keys = {o["key"] for o in resp.json()}
    assert keys == {"rpg_v1", "default_v1", "swdev_v1"}

    rpg = next(o for o in resp.json() if o["key"] == "rpg_v1")
    assert rpg["labels"]["tab.main"] == "Character Sheet"
    # The default overlay carries no role.* labels (persona labels live on the
    # workflow's persona_type_labels since the enterprise->default rename).
    default_overlay = next(o for o in resp.json() if o["key"] == "default_v1")
    assert default_overlay["labels"]["phase.brainstorm"] == "Brainstorm"
    swdev = next(o for o in resp.json() if o["key"] == "swdev_v1")
    assert swdev["labels"]["role.facilitator"] == "Engineering Manager"


async def test_workspace_resolves_to_system_default_with_no_override(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"vocab-default-{uuid.uuid4().hex[:8]}"
    _tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.get(f"/workspaces/{workspace_id}/vocabulary-overlay", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["key"] == "rpg_v1"


async def test_switching_a_workspace_to_default_v1_relabels_immediately(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"vocab-switch-{uuid.uuid4().hex[:8]}"
    _tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    overlays = client.get("/vocabulary-overlays", headers=headers).json()
    default_id = next(o["id"] for o in overlays if o["key"] == "default_v1")

    switch_resp = client.patch(
        f"/workspaces/{workspace_id}/vocabulary-overlay",
        json={"overlay_id": default_id},
        headers=headers,
    )
    assert switch_resp.status_code == 200, switch_resp.text
    assert switch_resp.json()["key"] == "default_v1"
    assert switch_resp.json()["labels"]["phase.brainstorm"] == "Brainstorm"

    get_resp = client.get(f"/workspaces/{workspace_id}/vocabulary-overlay", headers=headers)
    assert get_resp.json()["key"] == "default_v1"

    # Switching back to null clears the override -- falls back to the system default.
    clear_resp = client.patch(
        f"/workspaces/{workspace_id}/vocabulary-overlay",
        json={"overlay_id": None},
        headers=headers,
    )
    assert clear_resp.json()["key"] == "rpg_v1"


async def test_tenant_default_overlay_applies_when_no_workspace_override(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"vocab-tenant-default-{uuid.uuid4().hex[:8]}"
    _tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    set_resp = client.patch(
        "/tenant/vocabulary-overlay", json={"overlay_key": "default_v1"}, headers=headers
    )
    assert set_resp.status_code == 200, set_resp.text

    resolved = client.get(f"/workspaces/{workspace_id}/vocabulary-overlay", headers=headers)
    assert resolved.json()["key"] == "default_v1"


async def test_tenant_overlay_endpoint_resolves_without_a_workspace(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    """The shell's own labels (workspace list, persona picker, schema library) come
    from here: the tenant default when one is set, else the system default -- never
    whatever workspace was open last, and never the RPG defaults for a software
    organization."""
    slug = f"vocab-tenant-get-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    before = client.get("/tenant/vocabulary-overlay", headers=headers)
    assert before.status_code == 200, before.text
    assert before.json()["key"] == "rpg_v1"

    set_resp = client.patch(
        "/tenant/vocabulary-overlay", json={"overlay_key": "swdev_v1"}, headers=headers
    )
    assert set_resp.status_code == 200, set_resp.text

    after = client.get("/tenant/vocabulary-overlay", headers=headers)
    assert after.json()["key"] == "swdev_v1"
    assert after.json()["labels"]["role.facilitator"] == "Engineering Manager"


async def test_second_tenant_cannot_read_first_tenants_custom_overlay(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    from core.tenancy.scope import tenant_scope
    from core.vocabulary.models import VocabularyOverlayRow

    slug_a = f"vocab-tenant-a-{uuid.uuid4().hex[:8]}"
    slug_b = f"vocab-tenant-b-{uuid.uuid4().hex[:8]}"
    tenant_a, _owner_a, _workspace_a = await seed_dev_tenant(slug=slug_a)
    await seed_dev_tenant(slug=slug_b)

    async with tenant_scope(tenant_a) as session:
        session.add(
            VocabularyOverlayRow(
                tenant_id=tenant_a, key="custom_v1", name="Custom", labels={"entity.tenant": "X"}
            )
        )

    token_b = _register_and_login(client, slug_b)
    resp = client.get("/vocabulary-overlays", headers={"Authorization": f"Bearer {token_b}"})
    assert "custom_v1" not in {o["key"] for o in resp.json()}


async def test_vocabulary_endpoints_require_auth(client: TestClient, db_available: None) -> None:
    assert client.get("/vocabulary-overlays").status_code == 401
    assert client.get("/tenant/vocabulary-overlay").status_code == 401
    assert client.get(f"/workspaces/{uuid.uuid4()}/vocabulary-overlay").status_code == 401
