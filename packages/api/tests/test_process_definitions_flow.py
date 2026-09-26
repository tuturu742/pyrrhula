"""process-definition CRUD + validate endpoints over real HTTP."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
from core.process.dsl.fixtures import MINIMAL_MVP_FLOW, STANDARD_SESSION_FLOW
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


async def test_validate_endpoint_reports_issues_without_persisting(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"pd-validate-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    broken = {**MINIMAL_MVP_FLOW, "initial_phase": "does_not_exist"}
    resp = client.post(
        "/process-definitions/validate", json={"definition": broken}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is False
    assert any(i["field_path"] == "initial_phase" for i in body["issues"])

    listed = client.get("/process-definitions", headers=headers)
    assert listed.json() == []


async def test_validate_endpoint_reports_valid_for_a_good_definition(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"pd-validok-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.post(
        "/process-definitions/validate", json={"definition": MINIMAL_MVP_FLOW}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"valid": True, "issues": []}


async def test_create_get_list_round_trip(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"pd-crud-{uuid.uuid4().hex[:8]}"
    _tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = client.post(
        "/process-definitions",
        json={
            "key": "mvp",
            "name": "Minimal MVP Flow",
            "definition": MINIMAL_MVP_FLOW,
            "workspace_id": str(workspace_id),
        },
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    created = create_resp.json()
    assert created["version"] == 1

    get_resp = client.get(f"/process-definitions/{created['id']}", headers=headers)
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["id"] == created["id"]

    list_resp = client.get(
        "/process-definitions", headers=headers, params={"workspace_id": str(workspace_id)}
    )
    assert list_resp.status_code == 200, list_resp.text
    assert [d["id"] for d in list_resp.json()] == [created["id"]]


async def test_create_with_invalid_definition_returns_422_with_structured_issues(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"pd-create-invalid-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    broken = {**MINIMAL_MVP_FLOW, "initial_phase": "does_not_exist"}
    resp = client.post(
        "/process-definitions",
        json={"key": "broken", "name": "Broken", "definition": broken},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    assert any(i["field_path"] == "initial_phase" for i in resp.json()["detail"]["issues"])


async def test_templates_endpoint_serves_the_named_fixtures(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"pd-templates-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.get("/process-definitions/templates", headers=headers)
    assert resp.status_code == 200, resp.text
    templates = resp.json()
    keys = {t["key"] for t in templates}
    assert keys == {"standard_session_flow", "minimal_mvp_flow", "agent_round_table"}
    mvp = next(t for t in templates if t["key"] == "minimal_mvp_flow")
    assert mvp["definition"] == MINIMAL_MVP_FLOW


async def test_standard_session_flow_template_publishes_unmodified(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    """the acceptance criterion: "author the Standard Session Flow entirely in
    the UI; the saved JSON validates and runs a session." The editor's template gallery
    hands the author this exact fixture (served by /process-definitions/templates,
    verbatim from fixtures.py) as the starting canvas; publishing it unmodified through
    the same endpoint the "Publish" button calls proves that hand-off round-trips. the
    own interpreter tests (test_interpreter.py, test_awaits.py) already prove this same
    fixture actually runs a session end to end -- this test closes the remaining gap:
    that what the UI serves an author is exactly what the publish endpoint accepts.
    """
    slug = f"pd-standard-{uuid.uuid4().hex[:8]}"
    _tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    templates_resp = client.get("/process-definitions/templates", headers=headers)
    assert templates_resp.status_code == 200, templates_resp.text
    standard = next(t for t in templates_resp.json() if t["key"] == "standard_session_flow")
    assert standard["definition"] == STANDARD_SESSION_FLOW

    create_resp = client.post(
        "/process-definitions",
        json={
            "key": "standard_session_flow",
            "name": standard["name"],
            "definition": standard["definition"],
            "workspace_id": str(workspace_id),
        },
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    created = create_resp.json()
    assert created["version"] == 1
    # Not byte-identical to the fixture: model_dump() fills in field defaults the
    # fixture leaves implicit (e.g. budget.spill, actor.order) -- re-fetching and
    # re-validating the persisted document is the real round-trip proof.
    get_resp = client.get(f"/process-definitions/{created['id']}", headers=headers)
    assert get_resp.status_code == 200, get_resp.text
    revalidate_resp = client.post(
        "/process-definitions/validate",
        json={"definition": get_resp.json()["definition"]},
        headers=headers,
    )
    assert revalidate_resp.json() == {"valid": True, "issues": []}


async def test_definition_response_includes_created_at(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"pd-created-at-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = client.post(
        "/process-definitions",
        json={"key": "mvp", "name": "MVP", "definition": MINIMAL_MVP_FLOW},
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    assert create_resp.json()["created_at"] is not None


async def test_get_unknown_definition_returns_404(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"pd-404-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.get(f"/process-definitions/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404


async def test_second_tenant_cannot_read_first_tenants_definition(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug_a = f"pd-tenant-a-{uuid.uuid4().hex[:8]}"
    slug_b = f"pd-tenant-b-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug_a)
    await seed_dev_tenant(slug=slug_b)
    token_a = _register_and_login(client, slug_a)
    token_b = _register_and_login(client, slug_b)

    create_resp = client.post(
        "/process-definitions",
        json={"key": "mvp", "name": "MVP", "definition": MINIMAL_MVP_FLOW},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert create_resp.status_code == 201, create_resp.text
    definition_id = create_resp.json()["id"]

    resp = client.get(
        f"/process-definitions/{definition_id}", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 404
