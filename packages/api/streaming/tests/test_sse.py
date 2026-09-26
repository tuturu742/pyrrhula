"""SSE catch-up: reconnecting with ``Last-Event-ID`` replays
missed durable events. Exercises ``sse_stream()`` directly against a fake ``Request``
(headers + a disconnect signal) rather than over real HTTP/TestClient — deterministic,
no risk of hanging on the live-subscribe loop, which by design blocks indefinitely
waiting for new Redis messages.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from core.agents.seed import seed_dev_agent
from core.process.skeleton import create_session, submit_user_message
from core.tenancy.seed import seed_dev_tenant


def _fake_request(last_event_id: str | None, disconnect_after: int = 0) -> MagicMock:
    request = MagicMock()
    request.headers = {"last-event-id": last_event_id} if last_event_id else {}
    calls = {"n": 0}

    async def is_disconnected() -> bool:
        calls["n"] += 1
        return calls["n"] > disconnect_after

    request.is_disconnected = is_disconnected
    return request


async def test_reconnect_with_last_event_id_replays_missed_events(
    db_available: None, redis_available: None
) -> None:
    from api.streaming.sse import sse_stream

    tenant_id, owner_id, workspace_id = await seed_dev_tenant(slug=f"sse-{uuid.uuid4().hex[:8]}")
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    # Three durable events accumulate while the client is "disconnected" (never opened a
    # stream at all yet, in this test's telling).
    await submit_user_message(tenant_id, sess.id, owner_id, "first")
    await submit_user_message(tenant_id, sess.id, owner_id, "second")
    await submit_user_message(tenant_id, sess.id, owner_id, "third")

    # Reconnect claiming to have already seen event_seq 0 (the first message) -- catch-up
    # must replay events 1 and 2, not event 0 again, and must stop before blocking on the
    # live-subscribe loop (disconnect_after=0 breaks it on the first live-loop check).
    request = _fake_request(last_event_id="0", disconnect_after=0)
    collected = [chunk async for chunk in sse_stream(tenant_id, sess.id, request)]

    assert len(collected) == 2
    assert "second" in collected[0]
    assert "id: 1" in collected[0]
    assert "third" in collected[1]
    assert "id: 2" in collected[1]


async def test_reconnect_with_no_last_event_id_replays_everything(
    db_available: None, redis_available: None
) -> None:
    from api.streaming.sse import sse_stream

    tenant_id, owner_id, workspace_id = await seed_dev_tenant(slug=f"sse-{uuid.uuid4().hex[:8]}")
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    await submit_user_message(tenant_id, sess.id, owner_id, "only message")

    request = _fake_request(last_event_id=None, disconnect_after=0)
    collected = [chunk async for chunk in sse_stream(tenant_id, sess.id, request)]

    assert len(collected) == 1
    assert "only message" in collected[0]
