"""E2.9's slider surface: reading a persona's disposition (axes + current values) and
appending a new validated profile version over HTTP."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
from core.agents.seed import seed_dev_agent
from core.behavior.fixtures import RPG_AXIS_PACK, RPG_AXIS_PACK_ID
from core.behavior.repo import create_axis_definition
from core.behavior.validation import AxisDefinitionSchema
from core.tenancy.seed import seed_dev_tenant


@pytest.fixture
def client():  # noqa: ANN201
    with TestClient(app) as test_client:
        yield test_client


async def _setup() -> tuple[str, uuid.UUID]:
    slug = f"behav-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=slug)
    for definition in RPG_AXIS_PACK:
        await create_axis_definition(tenant_id, AxisDefinitionSchema.model_validate(definition))
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    return slug, persona_id


def _auth(client: TestClient, slug: str) -> dict[str, str]:
    email = f"{uuid.uuid4().hex}@example.com"
    response = client.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery", "display_name": "T"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert response.status_code == 200, response.text
    return {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "X-Pyrrhula-Tenant": slug,
    }


async def test_get_put_roundtrip_and_validation(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    await get_redis().delete("ratelimit:ip:testclient")
    slug, persona_id = await _setup()
    headers = _auth(client, slug)

    # GET before any profile: version 0, axes present with slider metadata.
    got = client.get(f"/agents/{persona_id}/behavior", headers=headers)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["version"] == 0
    axis_keys = {a["key"] for a in body["axes"]}
    assert "secret_disclosure_propensity" in axis_keys
    disclosure = next(a for a in body["axes"] if a["key"] == "secret_disclosure_propensity")
    assert disclosure["stakes"] == "high" and disclosure["has_gate"]
    assert disclosure["semantics_md"]  # the slider help text actually ships

    # PUT appends version 1.
    put = client.put(
        f"/agents/{persona_id}/behavior",
        json={
            "pack_id": RPG_AXIS_PACK_ID,
            "axis_values": {"secret_disclosure_propensity": 10, "talkativeness": 70},
        },
        headers=headers,
    )
    assert put.status_code == 200, put.text
    assert put.json()["version"] == 1
    assert put.json()["axis_values"]["secret_disclosure_propensity"] == 10

    # PUT again appends version 2 -- history, not overwrite.
    put2 = client.put(
        f"/agents/{persona_id}/behavior",
        json={"pack_id": RPG_AXIS_PACK_ID, "axis_values": {"secret_disclosure_propensity": 90}},
        headers=headers,
    )
    assert put2.json()["version"] == 2

    # Unknown axis and out-of-range are 422s, and no version is minted for them.
    bad_key = client.put(
        f"/agents/{persona_id}/behavior",
        json={"pack_id": RPG_AXIS_PACK_ID, "axis_values": {"charisma": 50}},
        headers=headers,
    )
    assert bad_key.status_code == 422 and "charisma" in bad_key.text
    bad_range = client.put(
        f"/agents/{persona_id}/behavior",
        json={"pack_id": RPG_AXIS_PACK_ID, "axis_values": {"talkativeness": 400}},
        headers=headers,
    )
    assert bad_range.status_code == 422
    assert client.get(f"/agents/{persona_id}/behavior", headers=headers).json()["version"] == 2
