"""the acceptance criteria over real HTTP through the FastAPI app -- create a secret,
prove plaintext access control, propose an AI-assist draft, and accept it as a new
version. Uses a scripted `ModelProvider` double (monkeypatched in, same pattern as
`test_sessions_flow.py`'s embedding-provider override) so no real model call happens.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from api.main import app
from api.redis_client import get_redis
from core.agents.authoring import create_agent
from core.audit.models import UsageRecordRow
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest
from core.secrets.drafting import SecretDraftResult
from core.tenancy.models import WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


@dataclass
class _ScriptedDraftProvider:
    result: SecretDraftResult

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def generate_structured(self, req: GenerationRequest, schema: type) -> object:  # type: ignore[type-arg]
        return self.result

    def count_tokens(self, text: str, model: str) -> int:
        return max(len(text.split()), 1)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            supports_tools=False, supports_json_mode=True, supports_prompt_caching=False
        )


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture
def client():  # noqa: ANN201
    with TestClient(app) as test_client:
        yield test_client


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
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal_id,
                role=role,
            )
        )


async def test_secret_content_requires_author_permission_and_participants_get_no_plaintext(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"secflow-perm-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)

    author_token, author_id = _register_and_login(client, slug)
    await _grant_role(tenant_id, workspace_id, author_id, "facilitator")
    participant_token, participant_id = _register_and_login(client, slug)
    await _grant_role(tenant_id, workspace_id, participant_id, "participant")

    author_headers = {"Authorization": f"Bearer {author_token}"}
    create_resp = client.post(
        "/secrets",
        json={
            "workspace_id": str(workspace_id),
            "subject_kind": "entity",
            "subject_id": str(uuid.uuid4()),
            "content": "the innkeeper is secretly the missing heir",
            "gist": "the innkeeper has a hidden identity",
            "scope_key": "workspace_public",
        },
        headers=author_headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    secret_id = create_resp.json()["id"]
    assert create_resp.json()["content"] == "the innkeeper is secretly the missing heir"

    author_get = client.get(f"/secrets/{secret_id}", headers=author_headers)
    assert author_get.status_code == 200
    assert author_get.json()["content"] == "the innkeeper is secretly the missing heir"
    assert author_get.json()["is_author"] is True

    participant_headers = {"Authorization": f"Bearer {participant_token}"}
    participant_get = client.get(f"/secrets/{secret_id}", headers=participant_headers)
    assert participant_get.status_code == 200
    body = participant_get.json()
    assert body["content"] is None
    assert body["hint_text"] is None
    assert body["behavioral_directive"] is None
    assert body["is_author"] is False
    assert body["gist"] == "the innkeeper has a hidden identity"

    # A participant may not create/edit secrets either -- same check, write side.
    participant_create = client.post(
        "/secrets",
        json={
            "workspace_id": str(workspace_id),
            "subject_kind": "entity",
            "subject_id": str(uuid.uuid4()),
            "content": "x",
            "gist": "y",
            "scope_key": "workspace_public",
        },
        headers=participant_headers,
    )
    assert participant_create.status_code == 403


async def test_ai_assist_writes_only_on_explicit_acceptance_as_a_new_version(
    client: TestClient,
    db_available: None,
    redis_available: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = f"secflow-draft-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    author_token, author_id = _register_and_login(client, slug)
    await _grant_role(tenant_id, workspace_id, author_id, "facilitator")
    headers = {"Authorization": f"Bearer {author_token}"}

    profile = await create_agent(
        tenant_id, "draft-profile", "echo", "echo-1", encryptor=IdentityEncryptor()
    )

    monkeypatch.setattr(
        "api.routes.secrets.get_model_provider",
        lambda _provider: _ScriptedDraftProvider(
            SecretDraftResult(
                behavioral_directive="grows quiet near the old bridge",
                hint_text="something about the crossing",
            )
        ),
    )

    create_resp = client.post(
        "/secrets",
        json={
            "workspace_id": str(workspace_id),
            "subject_kind": "entity",
            "subject_id": str(uuid.uuid4()),
            "content": "the bridge collapse was sabotage",
            "gist": "the bridge collapse was not an accident",
            "scope_key": "workspace_public",
        },
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    secret_id = create_resp.json()["id"]
    assert create_resp.json()["version"] == 1

    draft_resp = client.post(
        f"/secrets/{secret_id}/draft",
        json={"agent_id": str(profile.id)},
        headers=headers,
    )
    assert draft_resp.status_code == 200, draft_resp.text
    draft = draft_resp.json()
    assert draft["behavioral_directive"] == "grows quiet near the old bridge"

    # Proposing a draft never writes to the secret -- declining is simply not calling
    # PATCH; nothing to assert beyond "still version 1, still no directive".
    unchanged = client.get(f"/secrets/{secret_id}", headers=headers)
    assert unchanged.json()["version"] == 1
    assert unchanged.json()["behavioral_directive"] is None

    accept_resp = client.patch(
        f"/secrets/{secret_id}",
        json={
            "behavioral_directive": draft["behavioral_directive"],
            "hint_text": draft["hint_text"],
        },
        headers=headers,
    )
    assert accept_resp.status_code == 200, accept_resp.text
    assert accept_resp.json()["version"] == 2
    assert accept_resp.json()["behavioral_directive"] == "grows quiet near the old bridge"


async def test_ai_drafted_directive_containing_plaintext_is_rejected(
    client: TestClient,
    db_available: None,
    redis_available: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = f"secflow-leak-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    author_token, author_id = _register_and_login(client, slug)
    await _grant_role(tenant_id, workspace_id, author_id, "facilitator")
    headers = {"Authorization": f"Bearer {author_token}"}

    profile = await create_agent(
        tenant_id, "leak-profile", "echo", "echo-1", encryptor=IdentityEncryptor()
    )
    monkeypatch.setattr(
        "api.routes.secrets.get_model_provider",
        lambda _provider: _ScriptedDraftProvider(
            SecretDraftResult(
                behavioral_directive="never says the vault combination is thirty one seventeen",
                hint_text="something locked away",
            )
        ),
    )

    create_resp = client.post(
        "/secrets",
        json={
            "workspace_id": str(workspace_id),
            "subject_kind": "entity",
            "subject_id": str(uuid.uuid4()),
            "content": "the vault combination is thirty one seventeen forty two",
            "gist": "there is a vault with a combination",
            "scope_key": "workspace_public",
        },
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    secret_id = create_resp.json()["id"]

    draft_resp = client.post(
        f"/secrets/{secret_id}/draft",
        json={"agent_id": str(profile.id)},
        headers=headers,
    )
    assert draft_resp.status_code == 422, draft_resp.text

    unchanged = client.get(f"/secrets/{secret_id}", headers=headers)
    assert unchanged.json()["behavioral_directive"] is None
    assert unchanged.json()["version"] == 1


async def test_ai_assist_usage_record_written_in_transaction_with_purpose_rewrite(
    client: TestClient,
    db_available: None,
    redis_available: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = f"secflow-usage-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    author_token, author_id = _register_and_login(client, slug)
    await _grant_role(tenant_id, workspace_id, author_id, "facilitator")
    headers = {"Authorization": f"Bearer {author_token}"}

    profile = await create_agent(
        tenant_id, "usage-profile", "echo", "echo-1", encryptor=IdentityEncryptor()
    )
    monkeypatch.setattr(
        "api.routes.secrets.get_model_provider",
        lambda _provider: _ScriptedDraftProvider(
            SecretDraftResult(behavioral_directive="stays vague", hint_text="a small thing")
        ),
    )

    create_resp = client.post(
        "/secrets",
        json={
            "workspace_id": str(workspace_id),
            "subject_kind": "entity",
            "subject_id": str(uuid.uuid4()),
            "content": "an unremarkable fact",
            "gist": "there is a fact",
            "scope_key": "workspace_public",
        },
        headers=headers,
    )
    secret_id = create_resp.json()["id"]

    draft_resp = client.post(
        f"/secrets/{secret_id}/draft",
        json={"agent_id": str(profile.id)},
        headers=headers,
    )
    assert draft_resp.status_code == 200, draft_resp.text

    async with tenant_scope(tenant_id) as session:
        row = (
            await session.execute(
                select(UsageRecordRow).where(
                    UsageRecordRow.tenant_id == tenant_id, UsageRecordRow.purpose == "rewrite"
                )
            )
        ).scalar_one()
    assert row.agent_id == profile.id
