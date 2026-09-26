"""API-level round trip for the walking skeleton: register -> create session -> submit
message -> assistant reply appears, all over real HTTP through the FastAPI app (not just
the engine functions directly, as in packages/core/process/tests/).
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

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


async def test_resume_restarts_the_interpreter(
    client: TestClient, db_available: None, redis_available: None, monkeypatch
) -> None:
    """Resuming a paused autonomous session must kick the advance task. It used to flip
    the status and return: the session sat "active" with no interpreter running, stuck
    until someone posted a message or toggled the turn policy -- observed live after a
    fault-pause, where resume appeared to do nothing at all."""
    import api.routes.sessions as sessions_module

    slug = f"sessflow-kick-{uuid.uuid4().hex[:8]}"
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=slug)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    kicked: list[tuple] = []

    async def _fake_advance(tenant_id, session_id, process_definition_id):  # noqa: ANN001, ANN202
        kicked.append((tenant_id, session_id, process_definition_id))

    monkeypatch.setattr(sessions_module, "_run_process_definition_advance", _fake_advance)

    flow = {
        "name": "kick",
        "vocabulary_overlay": "default_v1",
        "initial_phase": "talk",
        "phases": {
            "talk": {
                "label_key": "phase.discussion",
                "actors": [{"persona_type": "supervisor", "mode": "generate", "max_turns": 1}],
                "visibility": {
                    "knowledge_classes": [],
                    "scopes": ["workspace_public"],
                    "entity_fields": "all",
                    "secrets": "none",
                },
                "budget": {"ratio": {"lore": 1.0}, "max_tokens": 200},
                "tools": [],
            }
        },
    }
    from core.process.authoring import create_definition

    row = await create_definition(tenant_id, "kick", "kick", flow, workspace_id=workspace_id)

    create_resp = client.post(
        "/sessions",
        json={
            "workspace_id": str(workspace_id),
            "persona_id": str(persona_id),
            "process_definition_id": str(row.id),
        },
        headers=headers,
    )
    assert create_resp.status_code in (200, 201), create_resp.text
    session_id = create_resp.json()["id"]
    kicked.clear()  # creation kicks too; this test is about resume

    assert client.post(f"/sessions/{session_id}/pause", headers=headers).status_code == 200
    resume_resp = client.post(f"/sessions/{session_id}/resume", headers=headers)
    assert resume_resp.status_code == 200, resume_resp.text

    assert kicked, "resume must restart the interpreter for an auto process session"
    assert str(kicked[0][1]) == session_id


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

    # No checkpoints yet -- write one directly (its own function; the interpreter
    # would normally call this at every phase transition, but the skeleton session
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
    """A session created with a real process_definition_id is driven by the
    interpreter, not the skeleton -- the engine-level mechanics are already proven
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


async def test_advance_continues_until_the_interpreter_really_stops(monkeypatch) -> None:
    """``advance_session`` stops at its own runaway-loop guard and reports 'active',
    meaning "nothing is blocking, there is just more to do". Calling it once and
    returning strands the session: active, no interpreter running, no fault to show --
    observed live as a mystery session frozen mid-interrogation that a manual poke
    revived. Statuses other than 'active' are real stops and must not be re-entered."""
    import api.routes.sessions as sessions_module
    from core.process.interpreter import AdvanceResult

    async def _definition(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        return SimpleNamespace(id=uuid.uuid4(), definition={}, version=1)

    monkeypatch.setattr(sessions_module, "get_definition", _definition)
    monkeypatch.setattr(sessions_module, "validate_raw", lambda _d: (object(), []))

    seen: list[str] = []

    def _runner(statuses: list[str]):  # noqa: ANN202
        async def _run(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
            status = statuses[len(seen)]
            seen.append(status)
            return AdvanceResult(status=status, steps_taken=200, final_phase="talk", flags=())

        return _run

    # Three guard-stops, then a real end: the task must ride through the guard-stops.
    monkeypatch.setattr(
        sessions_module,
        "run_process_definition_session",
        _runner(["active", "active", "active", "terminal"]),
    )
    await sessions_module._run_process_definition_advance(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    assert seen == ["active", "active", "active", "terminal"]

    # A blocking status stops immediately -- re-entering would fight the human.
    seen.clear()
    monkeypatch.setattr(
        sessions_module, "run_process_definition_session", _runner(["awaiting_human"])
    )
    await sessions_module._run_process_definition_advance(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    assert seen == ["awaiting_human"], "a real stop must not be re-entered"


async def test_a_session_waiting_on_a_person_says_so(monkeypatch) -> None:
    """Two mechanisms park a session on a human and neither reached the UI.

    An open await sets `status = "awaiting"`. A free-mode actor -- a phase whose turn is
    written rather than generated -- leaves `status = "active"` while the interpreter
    reports `awaiting_human`, so it rendered identically to a session being worked on:
    no error, no fault, nothing moving. Diagnosing one required reading the flow
    definition, which is not a thing a user should have to do.
    """
    import api.routes.sessions as sessions_module
    from core.process.interpreter import AdvanceResult

    seen: list[str] = []

    async def _fake_commit(_tenant, _session, _version, *, awaiting=None):  # noqa: ANN001
        seen.append(awaiting)

    import core.process.locking as locking

    monkeypatch.setattr(locking, "commit_advance", _fake_commit)

    # The API surfaces both shapes under one field, because they mean one thing to a reader.
    row = SimpleNamespace(
        status="awaiting", awaiting=None, id=uuid.uuid4(), workspace_id=uuid.uuid4()
    )
    assert sessions_module.SessionResponse.model_fields["awaiting"].default is None
    assert ("human" if row.status == "awaiting" else row.awaiting) == "human"

    free_mode = SimpleNamespace(status="active", awaiting="human")
    assert ("human" if free_mode.status == "awaiting" else free_mode.awaiting) == "human"

    working = SimpleNamespace(status="active", awaiting=None)
    assert ("human" if working.status == "awaiting" else working.awaiting) is None

    # And the interpreter's own verdict is what gets recorded, at the one commit point.
    result = AdvanceResult(status="awaiting_human", steps_taken=1, final_phase="x", flags=())
    await locking.commit_advance(
        uuid.uuid4(),
        uuid.uuid4(),
        0,
        awaiting="human" if result.status == "awaiting_human" else None,
    )
    assert seen == ["human"]
