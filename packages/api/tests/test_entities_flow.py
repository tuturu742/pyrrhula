"""The acceptance criterion over real HTTP: a private-tagged field is present in
the response for an authorized viewer and absent (not blanked) for one who isn't --
the wire contract the React sheet renders from.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
from core.entities.repo import save_schema
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import create_entity
from core.tenancy.models import WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


def _register_and_login(client: TestClient, slug: str) -> tuple[str, uuid.UUID]:
    email = f"{uuid.uuid4().hex}@example.com"
    response = client.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery", "display_name": "Tester"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    me = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    principal_id = uuid.UUID(me.json()["principal_id"])
    return token, principal_id


async def _grant_role(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, principal_id: uuid.UUID, role: str
) -> None:
    async with tenant_scope(tenant_id) as session:
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id, workspace_id=workspace_id, principal_id=principal_id, role=role
            )
        )


async def test_scope_filtered_fields_are_indistinguishable_from_absent(
    db_available: None, redis_available: None
) -> None:
    slug = f"entities-flow-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)

    with TestClient(app) as client:
        facilitator_token, facilitator_id = _register_and_login(client, slug)
        await _grant_role(tenant_id, workspace_id, facilitator_id, "facilitator")

        participant_token, participant_id = _register_and_login(client, slug)
        await _grant_role(tenant_id, workspace_id, participant_id, "participant")

        definition = EntitySchemaDefinition(
            fields=[
                FieldDef(key="name", type="string"),
                FieldDef(
                    key="secret_note", type="string", tags=["private"], scope_key="facilitator_only"
                ),
            ]
        )
        schema_row = await save_schema(tenant_id, workspace_id, "http-npc", 1, definition)
        entity = await create_entity(
            tenant_id,
            workspace_id,
            schema_row.id,
            definition,
            key=f"http-npc-{uuid.uuid4().hex[:8]}",
            name="HTTP NPC",
            scope_key="workspace_public",
            data={"name": "Visible", "secret_note": "hidden fact"},
        )

        facilitator_resp = client.get(
            f"/entities/{entity.id}",
            params={"workspace_id": str(workspace_id)},
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
        assert facilitator_resp.status_code == 200, facilitator_resp.text
        facilitator_keys = {f["key"] for f in facilitator_resp.json()["fields"]}
        assert "secret_note" in facilitator_keys

        participant_resp = client.get(
            f"/entities/{entity.id}",
            params={"workspace_id": str(workspace_id)},
            headers={"Authorization": f"Bearer {participant_token}"},
        )
        assert participant_resp.status_code == 200, participant_resp.text
        participant_keys = {f["key"] for f in participant_resp.json()["fields"]}
        assert "secret_note" not in participant_keys  # absent, not present-with-null
        assert "name" in participant_keys
