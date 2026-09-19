"""The preview WebSocket proxy, against a real socket on the other side.

Previews could serve a static site and nothing else. The tools that put a *running*
program in a browser -- ttyd for a terminal, noVNC for a desktop -- serve their page over
HTTP and then do all the work over a WebSocket on the same port, so a preview of a terminal
application or a desktop application was unreachable no matter what the recipe started.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import uuid

import pytest
import pytest_asyncio
import websockets
from fastapi.testclient import TestClient

from api.auth.tokens import issue_token
from api.main import app
from api.redis_client import get_redis
from core.previews.service import create_preview, mark_running, preview_name
from core.previews.tokens import mint_preview_token
from core.tenancy.seed import seed_dev_tenant


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture
def echo_server():  # noqa: ANN201
    """Stands in for ttyd: echoes text and bytes back, and negotiates a subprotocol.

    Runs on its own thread with its own loop on purpose. ``TestClient`` is synchronous and
    blocks the calling thread while the app runs in a portal of its own, so a server bound
    to the test's loop would simply never be serviced while a socket was open -- the
    connection would hang and then fail, which looks exactly like a broken proxy.
    """
    ready = threading.Event()
    state: dict[str, object] = {}

    async def handler(connection) -> None:  # noqa: ANN001
        async for message in connection:
            if isinstance(message, bytes):
                await connection.send(b"echo:" + message)
            else:
                await connection.send(f"echo:{message}")

    async def main() -> None:
        server = await websockets.serve(handler, "127.0.0.1", 0, subprotocols=["tty"])
        state["port"] = server.sockets[0].getsockname()[1]
        state["stop"] = asyncio.Event()
        ready.set()
        await state["stop"].wait()  # type: ignore[union-attr]
        server.close()
        await server.wait_closed()

    loop = asyncio.new_event_loop()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(timeout=10), "echo server never started"
    try:
        yield int(state["port"])  # type: ignore[call-overload]
    finally:
        loop.call_soon_threadsafe(state["stop"].set)  # type: ignore[union-attr]
        thread.join(timeout=5)


async def _running_preview(port: int) -> str:
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(slug=f"prevws-{uuid.uuid4().hex[:8]}")
    repo_id = uuid.uuid4()
    preview_id = await create_preview(
        tenant_id,
        name=preview_name(repo_id, "main"),
        repo_id=repo_id,
        workspace_id=workspace_id,
        session_id=None,
        artifact_name="app.tar.gz",
        engine_key=None,
        image="example/ttyd",
        ttl_seconds=600,
        git_ref="main",
        created_by_principal_id=owner_id,
    )
    await mark_running(
        tenant_id, preview_id, ref="pyr-prev-x", internal_url=f"http://127.0.0.1:{port}"
    )
    return mint_preview_token(tenant_id, preview_id, ttl_seconds=600)


@pytest.mark.asyncio
async def test_a_browser_socket_reaches_the_preview_container(echo_server: int) -> None:
    token = await _running_preview(echo_server)

    with (
        TestClient(app) as client,
        client.websocket_connect(f"/p/{token}/ws", subprotocols=["tty"]) as socket,
    ):
        socket.send_text("hello")
        assert socket.receive_text() == "echo:hello"
        socket.send_bytes(b"\x01\x02")
        assert socket.receive_bytes() == b"echo:\x01\x02"


@pytest.mark.asyncio
async def test_the_subprotocol_the_browser_offered_is_negotiated(echo_server: int) -> None:
    """ttyd refuses a connection that does not ask for `tty`, so the proxy has to carry the
    offer upstream and the acceptance back rather than negotiating on its own behalf."""
    token = await _running_preview(echo_server)

    with (
        TestClient(app) as client,
        client.websocket_connect(f"/p/{token}/ws", subprotocols=["tty"]) as socket,
    ):
        assert socket.accepted_subprotocol == "tty"


@pytest.mark.asyncio
async def test_an_unknown_token_is_refused_without_reaching_a_container() -> None:
    """Same silence as the HTTP side: guessing links must teach nothing."""
    from starlette.websockets import WebSocketDisconnect

    with (
        TestClient(app) as client,
        contextlib.suppress(WebSocketDisconnect),
        client.websocket_connect(f"/p/{'z' * 40}/ws"),
    ):
        pytest.fail("an invalid share token was accepted")


@pytest.mark.asyncio
async def test_a_dead_container_closes_the_socket_instead_of_hanging(echo_server: int) -> None:
    """A preview whose container is gone must fail the connection, not leave a browser tab
    waiting on a socket that will never speak."""
    from starlette.websockets import WebSocketDisconnect

    token = await _running_preview(echo_server + 1)  # nothing listening there

    with (
        TestClient(app) as client,
        contextlib.suppress(WebSocketDisconnect),
        client.websocket_connect(f"/p/{token}/ws"),
    ):
        await asyncio.sleep(0)
        pytest.fail("a socket to a dead container was accepted")


@pytest.mark.asyncio
async def test_creating_a_preview_over_http_reaches_the_queue() -> None:
    """Regression: `POST /previews` raised on every single call.

    Resolving the repo's `pyrrhula-preview.json` passed `store_key(repo.key)` where that
    helper takes `(tenant_id, repo_key)` -- tenant-prefixed, because repo keys are unique
    per tenant and a bare key would collide across them. So the configurable-preview
    feature shipped unable to start a preview at all, and nothing noticed because no test
    went through the endpoint: the pieces were covered, the call was not.
    """
    from core.repos.service import create_repo

    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=f"prevpost-{uuid.uuid4().hex[:8]}"
    )
    repo = await create_repo(
        tenant_id,
        key=f"game{uuid.uuid4().hex[:6]}",
        name="Game",
        created_by=owner_id,
        artifact_name="web.tgz",
    )

    token = issue_token(principal_id=owner_id, tenant_id=tenant_id)
    with TestClient(app) as client:
        response = client.post(
            "/previews",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "repo_id": str(repo.id),
                "workspace_id": str(workspace_id),
                "git_ref": "pyr/some-branch",
                "ttl_seconds": 300,
            },
        )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["git_ref"] == "pyr/some-branch"
    # The ref is part of the container's identity, so two branches are two previews.
    assert body["name"].startswith("pyr-prev-")
    assert body["name"] != f"pyr-prev-{str(repo.id)[:8]}"
