"""citation resolution is visible per message via API, at the pinned version."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.main import app
from api.redis_client import get_redis
from core.agents.seed import seed_dev_agent
from core.assembler.citations import apply_citation_validation, validate_citations
from core.knowledge.authoring import EntryFields, create_source, publish_version, upsert_draft_entry
from core.process.skeleton import create_session, submit_user_message
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


async def test_message_citations_endpoint_resolves_the_pinned_version(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"citations-api-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    message = await submit_user_message(tenant_id, sess.id, uuid.uuid4(), "irrelevant")

    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    await upsert_draft_entry(
        tenant_id,
        source.id,
        "grappling",
        EntryFields(
            title="Grappling", body_md="Pinned text", class_="rules", scope_key="workspace_public"
        ),
    )
    version = await publish_version(tenant_id, source.id, change_note="v1")

    manifest_entries = [
        {
            "citation_id": "k1",
            "entry_id": str(uuid.uuid4()),
            "entry_key": "grappling",
            "source_id": str(source.id),
            "version_id": str(version.id),
        }
    ]
    result = validate_citations("Per [k1], you succeed.", manifest_entries, requires_citation=False)
    await apply_citation_validation(tenant_id, message.id, result)

    # Publish again -- the endpoint must still resolve to the pinned version's text.
    await upsert_draft_entry(
        tenant_id,
        source.id,
        "grappling",
        EntryFields(
            title="Grappling", body_md="Newer text", class_="rules", scope_key="workspace_public"
        ),
    )
    await publish_version(tenant_id, source.id, change_note="v2")

    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get(f"/messages/{message.id}/citations", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body["valid"]) == 1
    assert body["valid"][0]["body_md"] == "Pinned text"
    assert body["valid"][0]["entry_key"] == "grappling"
    assert body["bad_citation_ids"] == []


async def test_message_citations_endpoint_surfaces_hallucinated_citation_ids(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"citations-bad-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    message = await submit_user_message(tenant_id, sess.id, uuid.uuid4(), "irrelevant")

    # The reply cites [k1], but the manifest that produced its context had no such entry
    # -- a hallucinated citation (the "hallucinated-citation flags
    # visible" acceptance criterion).
    result = validate_citations(
        "Per [k1], you succeed.", manifest_entries=[], requires_citation=False
    )
    await apply_citation_validation(tenant_id, message.id, result)

    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get(f"/messages/{message.id}/citations", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] == []
    assert body["bad_citation_ids"] == ["k1"]


async def test_message_citations_endpoint_requires_auth(
    client: TestClient, db_available: None
) -> None:
    response = client.get(f"/messages/{uuid.uuid4()}/citations")
    assert response.status_code == 401
