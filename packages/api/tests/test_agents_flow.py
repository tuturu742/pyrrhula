"""agent + model-profile management over real HTTP -- persona/role editing,
provider credentials that are never redisplayed, capability hints, and the create-two-
agents-that-both-take-turns acceptance criterion (proven here at the CRUD/data level;
the session view is what actually lets them take turns).
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


async def test_create_agent_never_returns_the_api_key(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"agents-key-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.post(
        "/model-profiles",
        json={
            "name": "Ollama local",
            "provider": "ollama",
            "model": "llama3",
            "api_key": "sk-super-secret-do-not-leak",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "api_key" not in body
    assert "sk-super-secret-do-not-leak" not in resp.text
    assert body["credential_ref"] is not None

    get_resp = client.get(f"/model-profiles/{body['id']}", headers=headers)
    assert get_resp.status_code == 200
    assert "sk-super-secret-do-not-leak" not in get_resp.text

    list_resp = client.get("/model-profiles", headers=headers)
    assert list_resp.status_code == 200
    assert "sk-super-secret-do-not-leak" not in list_resp.text


async def test_update_agent_params_without_supplying_a_key_again(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"agents-update-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = client.post(
        "/model-profiles",
        json={
            "name": "Ollama local",
            "provider": "ollama",
            "model": "llama3",
            "api_key": "sk-original-key",
        },
        headers=headers,
    )
    original_credential_ref = create_resp.json()["credential_ref"]

    update_resp = client.patch(
        f"/model-profiles/{create_resp.json()['id']}",
        json={"params": {"temperature": 0.7}},
        headers=headers,
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["params"] == {"temperature": 0.7}
    # credential_ref is untouched -- omitting api_key never disturbs the stored credential.
    assert update_resp.json()["credential_ref"] == original_credential_ref


async def test_capabilities_endpoint_reports_provider_hints(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"agents-caps-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.get(
        "/model-profiles/capabilities",
        params={"provider": "echo", "model": "echo-1"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "supports_tools": False,
        "supports_json_mode": False,
        "supports_prompt_caching": False,
    }


async def test_create_two_agents_with_different_roles_in_one_workspace(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"agents-two-{uuid.uuid4().hex[:8]}"
    _tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    profile_resp = client.post(
        "/model-profiles",
        json={"name": "Shared profile", "provider": "echo", "model": "echo-1"},
        headers=headers,
    )
    profile_id = profile_resp.json()["id"]

    arbiter_resp = client.post(
        "/agents",
        json={
            "workspace_id": str(workspace_id),
            "key": "arbiter",
            "name": "The Arbiter",
            "agent_id": profile_id,
            "persona_type": "supervisor",
            "persona_md": "A fair and even-handed narrator.",
        },
        headers=headers,
    )
    assert arbiter_resp.status_code == 201, arbiter_resp.text
    assert arbiter_resp.json()["persona_type"] == "supervisor"
    assert arbiter_resp.json()["persona_md"] == "A fair and even-handed narrator."

    participant_resp = client.post(
        "/agents",
        json={
            "workspace_id": str(workspace_id),
            "key": "npc-guard",
            "name": "Guard NPC",
            "agent_id": profile_id,
            "persona_type": "participant",
        },
        headers=headers,
    )
    assert participant_resp.status_code == 201, participant_resp.text
    assert participant_resp.json()["persona_type"] == "participant"

    list_resp = client.get("/agents", params={"workspace_id": str(workspace_id)}, headers=headers)
    assert list_resp.status_code == 200
    assert {a["key"] for a in list_resp.json()} == {"arbiter", "npc-guard"}


async def test_update_persona_can_explicitly_unlink_entity(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"agents-entity-{uuid.uuid4().hex[:8]}"
    _tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    profile_resp = client.post(
        "/model-profiles",
        json={"name": "P", "provider": "echo", "model": "echo-1"},
        headers=headers,
    )
    profile_id = profile_resp.json()["id"]
    entity_id = str(uuid.uuid4())

    agent_resp = client.post(
        "/agents",
        json={
            "workspace_id": str(workspace_id),
            "key": "npc",
            "name": "NPC",
            "agent_id": profile_id,
            "entity_id": entity_id,
        },
        headers=headers,
    )
    assert agent_resp.json()["entity_id"] == entity_id

    # Omitting entity_id in a PATCH leaves it alone.
    patch_noop = client.patch(
        f"/agents/{agent_resp.json()['id']}", json={"name": "NPC renamed"}, headers=headers
    )
    assert patch_noop.json()["entity_id"] == entity_id

    # Explicitly sending entity_id: null unlinks it.
    patch_unlink = client.patch(
        f"/agents/{agent_resp.json()['id']}", json={"entity_id": None}, headers=headers
    )
    assert patch_unlink.json()["entity_id"] is None


async def test_agents_endpoints_require_auth(client: TestClient, db_available: None) -> None:
    assert client.get("/agents", params={"workspace_id": str(uuid.uuid4())}).status_code == 401
    assert client.get("/model-profiles").status_code == 401


async def test_test_connection_endpoint_reports_success_for_a_working_provider(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"agents-testconn-ok-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = client.post(
        "/model-profiles",
        json={"name": "Echo", "provider": "echo", "model": "echo-1"},
        headers=headers,
    )
    profile_id = create_resp.json()["id"]

    resp = client.post(f"/model-profiles/{profile_id}/test", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert "echo/echo-1" in body["detail"]


async def test_test_connection_endpoint_reports_the_real_provider_error(
    client: TestClient,
    db_available: None,
    redis_available: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = f"agents-testconn-fail-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = client.post(
        "/model-profiles",
        json={"name": "Broken", "provider": "ollama", "model": "llama3"},
        headers=headers,
    )
    profile_id = create_resp.json()["id"]

    class _UnreachableProvider:
        async def generate(self, req: object):  # noqa: ANN001, ANN201
            raise ConnectionError("connection refused")
            yield  # pragma: no cover -- makes this an async generator

    monkeypatch.setattr(
        "api.routes.agents.get_model_provider", lambda _provider: _UnreachableProvider()
    )

    resp = client.post(f"/model-profiles/{profile_id}/test", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "connection refused" in body["detail"]


async def test_test_connection_endpoint_404s_for_an_unknown_profile(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"agents-testconn-404-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.post(f"/model-profiles/{uuid.uuid4()}/test", headers=headers)
    assert resp.status_code == 404


async def test_api_base_persists_updates_and_can_be_explicitly_cleared(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    """Connection setup lives on the profile, not a process-wide env var -- a tenant
    running two local Ollama deployments points different profiles at different hosts.
    Also proves the omitted-vs-null distinction PATCH needs: omitting api_base leaves it
    alone (like every other optional field here), but explicit null clears it back to
    "use the provider default" -- unlike fallback_agent_id, which cannot be cleared
    through this endpoint today."""
    slug = f"agents-apibase-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = client.post(
        "/model-profiles",
        json={
            "name": "Ollama local",
            "provider": "ollama",
            "model": "llama3",
            "api_base": "http://localhost:11434",
        },
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    profile_id = create_resp.json()["id"]
    assert create_resp.json()["api_base"] == "http://localhost:11434"

    # Omitting api_base in a PATCH leaves it alone.
    patch_noop = client.patch(
        f"/model-profiles/{profile_id}", json={"name": "Ollama local (renamed)"}, headers=headers
    )
    assert patch_noop.json()["api_base"] == "http://localhost:11434"

    # Changing it to a second deployment.
    patch_change = client.patch(
        f"/model-profiles/{profile_id}",
        json={"api_base": "http://localhost:22434"},
        headers=headers,
    )
    assert patch_change.json()["api_base"] == "http://localhost:22434"

    # Explicitly sending api_base: null clears it.
    patch_clear = client.patch(
        f"/model-profiles/{profile_id}", json={"api_base": None}, headers=headers
    )
    assert patch_clear.json()["api_base"] is None


async def test_test_connection_endpoint_passes_the_profiles_api_base_to_the_provider(
    client: TestClient,
    db_available: None,
    redis_available: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = f"agents-apibase-test-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = client.post(
        "/model-profiles",
        json={
            "name": "Second Ollama",
            "provider": "ollama",
            "model": "llama3",
            "api_base": "http://localhost:22434",
        },
        headers=headers,
    )
    profile_id = create_resp.json()["id"]

    seen_api_base: list[str | None] = []

    class _RecordingProvider:
        async def generate(self, req: object):  # noqa: ANN001, ANN201
            seen_api_base.append(req.api_base)  # type: ignore[attr-defined]
            return
            yield  # pragma: no cover -- makes this an async generator

    monkeypatch.setattr(
        "api.routes.agents.get_model_provider", lambda _provider: _RecordingProvider()
    )

    resp = client.post(f"/model-profiles/{profile_id}/test", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert seen_api_base == ["http://localhost:22434"]
