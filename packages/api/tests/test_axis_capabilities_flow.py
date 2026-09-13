"""E2.9's UI-facing acceptance criterion over real HTTP: a disabled axis control's API
response carries a machine-readable reason code, not just a bare disabled flag.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
from core.behavior.capabilities import upsert_capability
from core.behavior.fixtures import RPG_AXIS_PACK, RPG_AXIS_PACK_ID
from core.behavior.repo import create_axis_definition
from core.behavior.validation import AxisDefinitionSchema
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


async def test_disabled_axis_carries_reason_code_in_api_response(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"axiscap-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(slug=slug)
    for definition_dict in RPG_AXIS_PACK:
        await create_axis_definition(
            tenant_id, AxisDefinitionSchema.model_validate(definition_dict)
        )

    provider, model = "echo", "echo-disabled-1"
    await upsert_capability(
        provider,
        model,
        "secret_disclosure_propensity",
        capable=False,
        reason="structured-output fidelity below threshold",
    )

    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get(
        "/model-profiles/axis-capabilities",
        params={"provider": provider, "model": model, "pack_id": RPG_AXIS_PACK_ID},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()

    disabled = next(a for a in body if a["axis_key"] == "secret_disclosure_propensity")
    assert disabled["capable"] is False
    assert disabled["reason"] == "structured-output fidelity below threshold"

    # An axis with no stored eval result yet defaults to capable (permissive), with no
    # reason code needed -- nothing to disable.
    untested = next(a for a in body if a["axis_key"] == "chattiness")
    assert untested["capable"] is True
    assert untested["reason"] is None
