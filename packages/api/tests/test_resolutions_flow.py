"""D1.3/INV-7: a message's resolution records are visible per message via API, and the
contradiction badge rides along as a per-record flag -- never derived from prose.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
from core.agents.runtime import run_agent_turn
from core.agents.seed import seed_dev_agent
from core.agents.tools import ToolRegistry
from core.ports.model_provider import ToolCall, ToolSpec
from core.process.skeleton import create_session
from core.resolution.rule_system import MINIMAL_D20_SYSTEM, RuleSystemDefinition, create_rule_system
from core.resolution.service import make_randomizer_handler
from core.resolution.tests.test_tool_loop_integration import _ScriptedProvider, _ScriptedTurn
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


async def test_message_resolutions_endpoint_shows_the_record_and_the_contradiction_flag(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"resolutions-api-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    rule_system_row = await create_rule_system(tenant_id, MINIMAL_D20_SYSTEM)
    rule_system = RuleSystemDefinition.from_row(rule_system_row)

    async def actor_fields_resolver(actor_entity_id: uuid.UUID | None) -> dict[str, object]:
        return {"dexterity": 16}

    handler = make_randomizer_handler(
        rule_system=rule_system,
        rule_system_id=rule_system_row.id,
        legal_check_types=None,
        actor_fields_resolver=actor_fields_resolver,
    )
    registry = ToolRegistry()
    registry.register(ToolSpec(name="randomizer", description="Roll", parameters={}), handler)

    provider = _ScriptedProvider(
        turns=[
            _ScriptedTurn(
                text="",
                tool_calls=(
                    ToolCall(
                        id="call_1",
                        name="randomizer",
                        arguments={"expression": "1d20+3", "check_type": "stealth", "target": 100},
                    ),
                ),
            ),
            _ScriptedTurn(text="You succeed!"),  # contradicts -- max total is 23 < target 100
        ]
    )

    result = await run_agent_turn(
        tenant_id,
        persona_id,
        sess.id,
        [{"role": "user", "content": "I try to sneak past."}],
        model_provider_factory=lambda _name: provider,
        tool_registry=registry,
        idempotency_key=f"turn:{uuid.uuid4()}",
    )

    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get(f"/messages/{result.message_id}/resolutions", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    assert body[0]["outcome"] == "failure"  # the record's truth, not the reply's claim
    assert body[0]["contradicted"] is True


async def test_message_resolutions_endpoint_requires_auth(
    client: TestClient, db_available: None
) -> None:
    response = client.get(f"/messages/{uuid.uuid4()}/resolutions")
    assert response.status_code == 401


async def test_message_resolutions_endpoint_empty_for_a_message_with_no_rolls(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"resolutions-empty-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    provider = _ScriptedProvider(turns=[_ScriptedTurn(text="Just narration, no rolls.")])
    result = await run_agent_turn(
        tenant_id,
        persona_id,
        sess.id,
        [{"role": "user", "content": "hi"}],
        model_provider_factory=lambda _name: provider,
        tool_registry=ToolRegistry(),
        idempotency_key=f"turn:{uuid.uuid4()}",
    )

    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get(f"/messages/{result.message_id}/resolutions", headers=headers)
    assert response.status_code == 200
    assert response.json() == []
