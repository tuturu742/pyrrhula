"""G4.2 over real HTTP: the clock endpoints and the change feed. The core-level
acceptance criteria live in ``tests/isolation/test_between_session_state.py``; this file
covers the surface those criteria are reached through -- a route that 500s is not
something a service-level test would catch.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
from core.assembler.visibility import seed_default_scopes
from core.entities.repo import save_schema
from core.entities.schedule import create_schedule
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import create_entity
from core.tenancy.models import WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant
from worker.schedules import handle_apply_due_schedules


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
    return token, uuid.UUID(me.json()["principal_id"])


async def _grant_role(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, principal_id: uuid.UUID, role: str
) -> None:
    async with tenant_scope(tenant_id) as session:
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id, workspace_id=workspace_id, principal_id=principal_id, role=role
            )
        )


async def test_clock_advance_is_gated_enqueues_the_job_and_surfaces_in_the_change_feed(
    db_available: None, redis_available: None
) -> None:
    slug = f"clock-flow-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    await seed_default_scopes(tenant_id, workspace_id)

    definition = EntitySchemaDefinition(fields=[FieldDef(key="counter", type="integer")])
    schema_row = await save_schema(tenant_id, workspace_id, "http-ticker", 1, definition)
    entity = await create_entity(
        tenant_id,
        workspace_id,
        schema_row.id,
        definition,
        key=f"http-ticker-{uuid.uuid4().hex[:8]}",
        name="Ticker",
        scope_key="workspace_public",
        data={"counter": 0},
    )
    schedule = await create_schedule(
        tenant_id, workspace_id, entity.id, "upkeep", "at", 2, {"counter": 5}
    )

    with TestClient(app) as client:
        facilitator_token, facilitator_id = _register_and_login(client, slug)
        await _grant_role(tenant_id, workspace_id, facilitator_id, "facilitator")
        participant_token, participant_id = _register_and_login(client, slug)
        await _grant_role(tenant_id, workspace_id, participant_id, "participant")

        facilitator = {"Authorization": f"Bearer {facilitator_token}"}
        participant = {"Authorization": f"Bearer {participant_token}"}

        assert client.get(f"/workspaces/{workspace_id}/clock", headers=participant).json() == {
            "clock_value": 0
        }

        # A participant lives inside the timeline; moving it is the facilitator's act.
        denied = client.post(
            f"/workspaces/{workspace_id}/clock", json={"to_value": 4}, headers=participant
        )
        assert denied.status_code == 403, denied.text
        assert client.get(f"/workspaces/{workspace_id}/clock", headers=participant).json() == {
            "clock_value": 0
        }

        advanced = client.post(
            f"/workspaces/{workspace_id}/clock", json={"to_value": 4}, headers=facilitator
        )
        assert advanced.status_code == 202, advanced.text
        body = advanced.json()
        assert (body["from_clock"], body["to_clock"]) == (0, 4)

        # Rewinding is refused, and the clock is unchanged after the refusal.
        rewound = client.post(
            f"/workspaces/{workspace_id}/clock", json={"to_value": 1}, headers=facilitator
        )
        assert rewound.status_code == 409, rewound.text
        assert client.get(f"/workspaces/{workspace_id}/clock", headers=facilitator).json() == {
            "clock_value": 4
        }

        # The API only enqueued; nothing has been mutated yet. Drive the job through the
        # same handler the worker process would use.
        before = client.get(f"/workspaces/{workspace_id}/changes", headers=facilitator)
        assert before.status_code == 200, before.text
        assert before.json() == []

        result = await handle_apply_due_schedules(
            {
                "tenant_id": str(tenant_id),
                "workspace_id": str(workspace_id),
                "principal_id": str(facilitator_id),
                "from_clock": 0,
                "to_clock": 4,
            }
        )
        assert result["count"] == 1

        feed = client.get(
            f"/workspaces/{workspace_id}/changes?out_of_session_only=true", headers=facilitator
        )
        assert feed.status_code == 200, feed.text
        rows = feed.json()
        assert len(rows) == 1
        assert rows[0]["cause"] == "fsm"
        assert rows[0]["cause_ref"] == f"schedule:{schedule.id}:2"
        assert rows[0]["in_session"] is False
        assert rows[0]["new_value"] == 5
