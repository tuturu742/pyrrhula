"""The assistant chat's contract: read tools execute, write tools only PROPOSE (no
server-side write), and failures surface as stream events -- never hangs, never raises
out of the stream. Live Postgres + scripted providers (the ``test_editing`` pattern)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from adapters.embedding.stub.provider import StubEmbeddingProvider
from adapters.encryptor.identity import IdentityEncryptor
from core.agents.assistant import ensure_workspace_assistant
from core.agents.assistant_chat import chat
from core.ports.model_provider import (
    Capabilities,
    Chunk,
    GenerationRequest,
    ToolCall,
)
from core.sessions.lifecycle import get_session
from core.tenancy.seed import seed_dev_tenant


@dataclass
class _ScriptedChatProvider:
    """Yields the queued (text, tool_calls) turns, one per generate() call."""

    turns: list[tuple[str, tuple[ToolCall, ...]]]
    calls: int = 0
    seen_tools: list[tuple[str, ...]] = field(default_factory=list)

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        self.seen_tools.append(tuple(t.name for t in req.tools))
        text, tool_calls = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        if text:
            yield Chunk(text=text)
        yield Chunk(text="", finish_reason="stop", tool_calls=tool_calls)

    async def generate_structured(self, req: GenerationRequest, schema: type) -> object:  # type: ignore[type-arg]
        raise NotImplementedError

    def count_tokens(self, text: str, model: str) -> int:
        return max(len(text.split()), 1)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            supports_tools=True, supports_json_mode=True, supports_prompt_caching=False
        )


async def _setup(prefix: str):  # noqa: ANN202
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{prefix}-{uuid.uuid4().hex[:8]}"
    )
    await ensure_workspace_assistant(tenant_id, workspace_id)
    from core.tenancy.models import Principal
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(tenant_id) as session:
        viewer = await session.get(Principal, owner_id)
        assert viewer is not None
        session.expunge(viewer)
    return tenant_id, workspace_id, viewer


async def _collect(events: AsyncIterator[dict]) -> list[dict]:
    return [e async for e in events]


async def test_plain_answer_streams_text_and_done(db_available: None) -> None:
    tenant_id, workspace_id, viewer = await _setup("chat-plain")
    provider = _ScriptedChatProvider(turns=[("Hello! Ask me anything.", ())])

    events = await _collect(
        chat(
            tenant_id,
            workspace_id,
            viewer,
            [{"role": "user", "content": "hi"}],
            embedder=StubEmbeddingProvider(dimension=1024),
            provider_factory=lambda _p: provider,
            encryptor=IdentityEncryptor(),
        )
    )

    kinds = [e["type"] for e in events]
    assert kinds[-1] == "done"
    assert (
        "".join(e.get("delta", "") for e in events if e["type"] == "text")
        == "Hello! Ask me anything."
    )
    # The full tool catalog was offered to the model.
    assert "list_personas" in provider.seen_tools[0]
    assert "rename_session" in provider.seen_tools[0]


async def test_read_tool_executes_inline(db_available: None) -> None:
    tenant_id, workspace_id, viewer = await _setup("chat-read")
    provider = _ScriptedChatProvider(
        turns=[
            ("", (ToolCall(id="c1", name="list_personas", arguments={}),)),
            ("You have an assistant persona.", ()),
        ]
    )

    events = await _collect(
        chat(
            tenant_id,
            workspace_id,
            viewer,
            [{"role": "user", "content": "what personas exist?"}],
            embedder=StubEmbeddingProvider(dimension=1024),
            provider_factory=lambda _p: provider,
            encryptor=IdentityEncryptor(),
        )
    )

    assert {"type": "tool", "name": "list_personas"} in events
    assert events[-1]["type"] == "done"
    assert provider.calls == 2  # tool round + final answer


async def test_write_tool_proposes_and_does_not_execute(db_available: None) -> None:
    tenant_id, workspace_id, viewer = await _setup("chat-write")
    from core.process.skeleton import create_session

    persona = await ensure_workspace_assistant(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona.id)
    provider = _ScriptedChatProvider(
        turns=[
            (
                "",
                (
                    ToolCall(
                        id="c1",
                        name="rename_session",
                        arguments={"session_id": str(sess.id), "name": "Sprint kickoff"},
                    ),
                ),
            ),
            ("I proposed the rename for your confirmation.", ()),
        ]
    )

    events = await _collect(
        chat(
            tenant_id,
            workspace_id,
            viewer,
            [{"role": "user", "content": "rename that session to Sprint kickoff"}],
            embedder=StubEmbeddingProvider(dimension=1024),
            provider_factory=lambda _p: provider,
            encryptor=IdentityEncryptor(),
        )
    )

    proposals = [e for e in events if e["type"] == "proposal"]
    assert len(proposals) == 1
    assert proposals[0]["action"] == "rename_session"
    assert proposals[0]["args"]["name"] == "Sprint kickoff"
    # NOT executed server-side: the session's name is untouched.
    row = await get_session(tenant_id, sess.id)
    assert row is not None and row.name is None
    assert events[-1]["type"] == "done"


async def test_provider_failure_becomes_error_event(db_available: None) -> None:
    tenant_id, workspace_id, viewer = await _setup("chat-error")

    class _Boom(_ScriptedChatProvider):
        async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
            raise RuntimeError("provider exploded")
            yield  # pragma: no cover

    events = await _collect(
        chat(
            tenant_id,
            workspace_id,
            viewer,
            [{"role": "user", "content": "hi"}],
            embedder=StubEmbeddingProvider(dimension=1024),
            provider_factory=lambda _p: _Boom(turns=[]),
            encryptor=IdentityEncryptor(),
        )
    )

    assert events[-1]["type"] == "error"
    assert "provider exploded" in events[-1]["detail"]
