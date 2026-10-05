"""The library tenant over real HTTP: editing a library entry as tenant B forks it into B's own
tenant and edits the fork, leaving the library copy untouched; register/login against the
library tenant's own slug is refused.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text

from api.main import app
from api.redis_client import get_redis
from api.tests.grants import grant_tenant_role
from core.knowledge.authoring import EntryFields, create_source, publish_version, upsert_draft_entry
from core.knowledge.library import LIBRARY_TENANT_ID
from core.knowledge.models import KnowledgeSource
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


async def _seed_library_source() -> uuid.UUID:
    source = await create_source(
        LIBRARY_TENANT_ID, key=f"lib-{uuid.uuid4().hex[:8]}", name="Library Source", class_="rules"
    )
    await upsert_draft_entry(
        LIBRARY_TENANT_ID,
        source.id,
        "grappling",
        EntryFields(
            title="Grappling", body_md="original", class_="rules", scope_key="workspace_public"
        ),
    )
    await publish_version(LIBRARY_TENANT_ID, source.id)
    return source.id


async def test_editing_a_library_entry_forks_it_and_leaves_the_library_copy_untouched(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    library_source_id = await _seed_library_source()

    slug = f"kn-lib-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    await grant_tenant_role(client, token, tenant_id, "editor")
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.put(
        f"/knowledge/sources/{library_source_id}/entries/grappling",
        json={
            "title": "Grappling",
            "body_md": "tenant B's edit",
            "class": "rules",
            "scope_key": "workspace_public",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["forked_source_id"] is not None
    forked_source_id = uuid.UUID(body["forked_source_id"])
    assert forked_source_id != library_source_id

    # The fork belongs to tenant B and carries the edit.
    async with tenant_scope(tenant_id) as session:
        forked = await session.get(KnowledgeSource, forked_source_id)
    assert forked is not None
    assert forked.tenant_id == tenant_id

    list_resp = client.get(f"/knowledge/sources/{forked_source_id}/entries", headers=headers)
    assert list_resp.status_code == 200, list_resp.text
    [grappling] = [e for e in list_resp.json() if e["entry_key"] == "grappling"]
    assert grappling["body_md"] == "tenant B's edit"

    # The library's own copy is untouched.
    async with tenant_scope(LIBRARY_TENANT_ID) as session:
        library_body = (
            await session.execute(
                text(
                    "SELECT body_md FROM knowledge_entry WHERE knowledge_source_id = :sid "
                    "AND entry_key = 'grappling' AND version_id IS NULL"
                ),
                {"sid": library_source_id},
            )
        ).scalar_one()
    assert library_body == "original"


async def test_register_against_the_library_tenant_slug_is_refused(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    resp = client.post(
        "/auth/register",
        json={
            "email": f"{uuid.uuid4().hex}@example.com",
            "password": "correct horse battery",
            "display_name": "Tester",
        },
        headers={"X-Pyrrhula-Tenant": "pyrrhula-library"},
    )
    assert resp.status_code == 404


async def test_login_against_the_library_tenant_slug_is_refused(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    resp = client.post(
        "/auth/login",
        json={"email": "nobody@example.com", "password": "irrelevant"},
        headers={"X-Pyrrhula-Tenant": "pyrrhula-library"},
    )
    assert resp.status_code == 404
