"""the acceptance criteria over real HTTP: the template gallery, live CEL
validation (never persisting), and creating a schema from a template.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
from core.packs.loader import load_pack
from core.tenancy.seed import seed_dev_tenant

_RPG_PACK_DIR = pathlib.Path(__file__).resolve().parents[3] / ".plugins" / "default" / "rpg"


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


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


async def test_template_first_flow_produces_renderable_schema(
    db_available: None, redis_available: None
) -> None:
    slug = f"schema-editor-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    # workspace_id omitted (defaults to None): pack schemas load as templates
    # (workspace_id IS NULL), not tied to this one workspace -- the template
    # gallery's own read path (`list_latest_schemas(tenant_id, None)`).
    await load_pack(_RPG_PACK_DIR, tenant_id)

    with TestClient(app) as client:
        token = _register_and_login(client, slug)
        headers = {"Authorization": f"Bearer {token}"}

        templates_resp = client.get("/entities/schemas/templates", headers=headers)
        assert templates_resp.status_code == 200, templates_resp.text
        templates = templates_resp.json()
        character_template = next(t for t in templates if t["key"] == "character")

        # "Create from template" -- the exact template definition, saved into the
        # caller's own workspace, with no further edits.
        create_resp = client.post(
            "/entities/schemas",
            json={
                "key": "character",
                "workspace_id": str(workspace_id),
                "definition": character_template["definition"],
            },
            headers=headers,
        )
        assert create_resp.status_code == 201, create_resp.text
        created = create_resp.json()
        assert created["workspace_id"] == str(workspace_id)
        assert created["definition"]["fields"] == character_template["definition"]["fields"]

        # Immediately renderable -- fetching it back yields the same, valid definition
        # the SheetView can render without further edits.
        get_resp = client.get(f"/entities/schemas/{created['id']}", headers=headers)
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["definition"]["fields"] == character_template["definition"]["fields"]


async def test_live_cel_validation_blocks_save_with_anchored_error(
    db_available: None, redis_available: None
) -> None:
    slug = f"schema-editor-cel-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)

    with TestClient(app) as client:
        token = _register_and_login(client, slug)
        headers = {"Authorization": f"Bearer {token}"}

        bad_definition = {
            "fields": [{"key": "xp", "type": "integer"}],
            "derived": [
                {"key": "level", "type": "integer", "expression": "fields.nonexistent + 1"}
            ],
        }

        # Live validation (no persistence): the bad expression is named, anchored to
        # its exact field path.
        validate_resp = client.post(
            "/entities/schemas/validate", json={"definition": bad_definition}, headers=headers
        )
        assert validate_resp.status_code == 200, validate_resp.text
        body = validate_resp.json()
        assert body["valid"] is False
        assert any(issue["field_path"] == "derived[0].expression" for issue in body["issues"])
        assert any("nonexistent" in issue["message"] for issue in body["issues"])

        # Save is blocked -- nothing gets persisted for an invalid schema.
        create_resp = client.post(
            "/entities/schemas",
            json={"key": "broken", "workspace_id": str(workspace_id), "definition": bad_definition},
            headers=headers,
        )
        assert create_resp.status_code == 422
        assert any(
            issue["field_path"] == "derived[0].expression"
            for issue in create_resp.json()["detail"]["issues"]
        )

        list_resp = client.get(
            "/entities/schemas", params={"workspace_id": str(workspace_id)}, headers=headers
        )
        assert list_resp.status_code == 200
        assert all(s["key"] != "broken" for s in list_resp.json())
