"""API-level round trip for T0.8's walking skeleton: register -> create session -> submit
message -> assistant reply appears, all over real HTTP through the FastAPI app (not just
the engine functions directly, as in packages/core/process/tests/).
"""

from __future__ import annotations

import time
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from adapters.embedding.stub.provider import StubEmbeddingProvider
from api.main import app
from api.redis_client import get_redis
from core.agents.seed import seed_dev_agent
from core.process.authoring import create_definition
from core.process.dsl.fixtures import MINIMAL_MVP_FLOW
from core.tenancy.models import Principal, WorkspaceMembership
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


async def test_full_http_round_trip_produces_assistant_reply(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"sessflow-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)

    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = client.post(
        "/sessions",
        json={"workspace_id": str(workspace_id), "persona_id": str(persona_id)},
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    session_id = create_resp.json()["id"]
    assert create_resp.json()["current_phase"] == "prompt"

    submit_resp = client.post(
        f"/sessions/{session_id}/messages",
        json={"content": "hello from the http test"},
        headers=headers,
    )
    assert submit_resp.status_code == 202
    assert submit_resp.json() == {"accepted": True}

    # TestClient drives the background task to completion within the request lifecycle
    # (FastAPI awaits background tasks before the ASGI response cycle for that request
    # finishes), so the assistant's reply should already be persisted -- verified via a
    # short poll for robustness rather than assuming zero latency.
    from sqlalchemy import select

    from core.sessions.models import MessageRow
    from core.tenancy.scope import tenant_scope

    deadline = time.monotonic() + 5.0
    messages: list[object] = []
    while time.monotonic() < deadline:
        async with tenant_scope(tenant_id) as session:
            messages = (
                (
                    await session.execute(
                        select(MessageRow)
                        .where(MessageRow.session_id == uuid.UUID(session_id))
                        .order_by(MessageRow.event_seq)
                    )
                )
                .scalars()
                .all()
            )
        if len(messages) >= 2:
            break
        time.sleep(0.05)

    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[1].role == "assistant"
    assert "hello from the http test" in messages[1].content_md


async def test_get_session_endpoint_returns_the_current_snapshot(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"sessflow-get-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = client.post(
        "/sessions",
        json={"workspace_id": str(workspace_id), "persona_id": str(persona_id)},
        headers=headers,
    )
    session_id = create_resp.json()["id"]

    get_resp = client.get(f"/sessions/{session_id}", headers=headers)
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["id"] == session_id
    assert get_resp.json()["status"] == "active"


async def test_pause_and_resume_session_endpoints(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"sessflow-pause-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = client.post(
        "/sessions",
        json={"workspace_id": str(workspace_id), "persona_id": str(persona_id)},
        headers=headers,
    )
    session_id = create_resp.json()["id"]

    pause_resp = client.post(f"/sessions/{session_id}/pause", headers=headers)
    assert pause_resp.status_code == 200, pause_resp.text
    assert pause_resp.json()["status"] == "paused"

    resume_resp = client.post(f"/sessions/{session_id}/resume", headers=headers)
    assert resume_resp.status_code == 200, resume_resp.text
    assert resume_resp.json()["status"] == "active"


async def test_checkpoints_and_fork_endpoints(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    from core.process.checkpoints import write_checkpoint

    slug = f"sessflow-fork-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = client.post(
        "/sessions",
        json={"workspace_id": str(workspace_id), "persona_id": str(persona_id)},
        headers=headers,
    )
    session_id = create_resp.json()["id"]

    # No checkpoints yet -- write one directly (B1.4's own function; the interpreter
    # would normally call this at every phase transition, but the T0.8-skeleton session
    # created above never transitions through the real interpreter).
    await write_checkpoint(tenant_id, uuid.UUID(session_id), workspace_id)

    list_resp = client.get(f"/sessions/{session_id}/checkpoints", headers=headers)
    assert list_resp.status_code == 200, list_resp.text
    checkpoints = list_resp.json()
    assert len(checkpoints) == 1
    checkpoint_id = checkpoints[0]["id"]

    fork_resp = client.post(
        f"/sessions/{session_id}/fork",
        json={"checkpoint_id": checkpoint_id},
        headers=headers,
    )
    assert fork_resp.status_code == 201, fork_resp.text
    forked = fork_resp.json()
    assert forked["id"] != session_id
    assert forked["workspace_id"] == str(workspace_id)


async def test_process_definition_backed_session_runs_through_the_real_http_endpoints(
    client: TestClient, db_available: None, redis_available: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1.8: a session created with a real process_definition_id is driven by the
    interpreter, not the T0.8 skeleton -- the engine-level mechanics are already proven
    exhaustively by core/process/tests/test_live_session.py; this proves the same thing
    is reachable through the actual HTTP surface a client uses."""
    monkeypatch.setattr(
        "api.routes.sessions.get_embedding_provider",
        lambda: StubEmbeddingProvider(dimension=1024),
    )

    slug = f"sessflow-live-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(
        tenant_id, workspace_id, key="facilitator", provider="echo", persona_type="supervisor"
    )
    definition_row = await create_definition(tenant_id, "mvp-http", "MVP HTTP", MINIMAL_MVP_FLOW)

    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}
    async with tenant_scope(tenant_id) as session:
        human_principal = Principal(tenant_id=tenant_id, kind="human", display_name="Player")
        session.add(human_principal)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=human_principal.id,
                role="participant",
            )
        )

    create_resp = client.post(
        "/sessions",
        json={
            "workspace_id": str(workspace_id),
            "persona_id": str(persona_id),
            "process_definition_id": str(definition_row.id),
        },
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    session_id = create_resp.json()["id"]
    assert create_resp.json()["process_definition_id"] == str(definition_row.id)

    # create_session_endpoint's own background task already kicked off the first
    # advance (arbiter_narrate needs no human input) -- poll for it to land.
    deadline = time.monotonic() + 5.0
    get_resp = None
    while time.monotonic() < deadline:
        get_resp = client.get(f"/sessions/{session_id}", headers=headers)
        if get_resp.json()["current_phase"] == "player_act":
            break
        time.sleep(0.05)
    assert get_resp is not None
    assert get_resp.json()["current_phase"] == "player_act"
    assert get_resp.json()["status"] == "active"

    submit_resp = client.post(
        f"/sessions/{session_id}/messages",
        json={"content": "I look around the room."},
        headers=headers,
    )
    assert submit_resp.status_code == 202

    # Give the background task a moment, then check the durable log via the SSE
    # catch-up path directly -- not client.get(".../stream") over TestClient, which
    # would hang on the live-subscribe loop's indefinite wait (see
    # api/streaming/tests/test_sse.py's own docstring on exactly this).
    from unittest.mock import MagicMock

    from api.streaming.sse import sse_stream

    def _fake_request() -> MagicMock:
        request = MagicMock()
        request.headers = {}
        calls = {"n": 0}

        async def is_disconnected() -> bool:
            calls["n"] += 1
            return calls["n"] > 0

        request.is_disconnected = is_disconnected
        return request

    deadline = time.monotonic() + 5.0
    events: list[str] = []
    while time.monotonic() < deadline:
        events = [
            chunk async for chunk in sse_stream(tenant_id, uuid.UUID(session_id), _fake_request())
        ]
        if sum(e.count("event: message") for e in events) >= 3:
            break
        time.sleep(0.05)

    body = "".join(events)
    assert body.count("event: message") >= 3  # arbiter, human, arbiter's resolve reply
    assert "I look around the room." in body
    assert "phase_transition" in body


async def test_session_endpoints_require_auth(client: TestClient, db_available: None) -> None:
    response = client.post(
        "/sessions", json={"workspace_id": str(uuid.uuid4()), "persona_id": str(uuid.uuid4())}
    )
    assert response.status_code == 401


async def test_second_tenant_cannot_reach_first_tenants_session_via_api(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug_a = f"sessflow-a-{uuid.uuid4().hex[:8]}"
    slug_b = f"sessflow-b-{uuid.uuid4().hex[:8]}"
    tenant_a, _owner_a, workspace_a = await seed_dev_tenant(slug=slug_a)
    await seed_dev_tenant(slug=slug_b)
    agent_a = await seed_dev_agent(tenant_a, workspace_a)

    token_a = _register_and_login(client, slug_a)
    session_resp = client.post(
        "/sessions",
        json={"workspace_id": str(workspace_a), "persona_id": str(agent_a)},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    session_id = session_resp.json()["id"]

    token_b = _register_and_login(client, slug_b)
    submit_resp = client.post(
        f"/sessions/{session_id}/messages",
        json={"content": "trying to reach tenant A's session"},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    # tenant B's RLS scope simply has no such session -- submit_user_message's
    # session.get() returns None and the route surfaces that as 404, the same response a
    # genuinely nonexistent session_id would get (RLS makes the two indistinguishable,
    # and the HTTP layer shouldn't leak the distinction either).
    assert submit_resp.status_code == 404

    from sqlalchemy import select

    from core.sessions.models import MessageRow
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(tenant_a) as session:
        messages = (
            await session.execute(
                select(MessageRow).where(MessageRow.session_id == uuid.UUID(session_id))
            )
        ).all()
    assert messages == []
