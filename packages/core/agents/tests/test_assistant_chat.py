"""The assistant chat's contract: read tools execute, write tools only PROPOSE (no
server-side write), and failures surface as stream events -- never hangs, never raises
out of the stream. Live Postgres + scripted providers (the ``test_editing`` pattern)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from adapters.embedding.stub.provider import StubEmbeddingProvider
from adapters.encryptor.identity import IdentityEncryptor
from adapters.permission.role_permission import RolePermissionService
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
    seen_messages: list[list[dict]] = field(default_factory=list)

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        self.seen_tools.append(tuple(t.name for t in req.tools))
        self.seen_messages.append([dict(m) for m in req.messages])
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
    persona = await ensure_workspace_assistant(tenant_id, workspace_id)
    from core.agents.models import Agent
    from core.tenancy.models import Principal
    from core.tenancy.scope import tenant_scope

    # A fresh deployment configures no model (Settings.assistant_model is empty), so the
    # assistant is created without one and chat refuses until an operator picks one.
    # These tests are about chat behaviour with a configured assistant, so configure it --
    # the unconfigured path has its own test below.
    async with tenant_scope(tenant_id) as session:
        profile = await session.get(Agent, persona.agent_id)
        assert profile is not None
        profile.provider, profile.model = "openai", "gpt-4o-mini"
    async with tenant_scope(tenant_id) as session:
        viewer = await session.get(Principal, owner_id)
        assert viewer is not None
        session.expunge(viewer)
    # Signup makes the owner a steward of the first workspace (routes/auth.py); the dev
    # seed does not, and the read tools now answer under the viewer's workspace role.
    await _make_steward(tenant_id, workspace_id, owner_id)
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
            permission_service=RolePermissionService(),
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
            permission_service=RolePermissionService(),
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
                        arguments={"session_id": str(sess.id), "name": "Quarter kickoff"},
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
            [{"role": "user", "content": "rename that session to Quarter kickoff"}],
            embedder=StubEmbeddingProvider(dimension=1024),
            provider_factory=lambda _p: provider,
            encryptor=IdentityEncryptor(),
            permission_service=RolePermissionService(),
        )
    )

    proposals = [e for e in events if e["type"] == "proposal"]
    assert len(proposals) == 1
    assert proposals[0]["action"] == "rename_session"
    assert proposals[0]["args"]["name"] == "Quarter kickoff"
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
            permission_service=RolePermissionService(),
        )
    )

    assert events[-1]["type"] == "error"
    assert "provider exploded" in events[-1]["detail"]


async def test_chat_refuses_when_no_model_is_configured(db_available: None) -> None:
    """A clean install ships no assistant model. The stream must say so in words an
    operator can act on, rather than dying later inside the provider on a connection to
    a host nobody configured."""
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=f"chat-nomodel-{uuid.uuid4().hex[:8]}"
    )
    await ensure_workspace_assistant(tenant_id, workspace_id)
    from core.tenancy.models import Principal
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(tenant_id) as session:
        viewer = await session.get(Principal, owner_id)
        assert viewer is not None
        session.expunge(viewer)

    events = await _collect(
        chat(
            tenant_id,
            workspace_id,
            viewer,
            [{"role": "user", "content": "hi"}],
            embedder=StubEmbeddingProvider(dimension=1024),
            provider_factory=lambda _p: _ScriptedChatProvider(turns=[("unused", ())]),
            encryptor=IdentityEncryptor(),
            permission_service=RolePermissionService(),
        )
    )
    assert events[-1]["type"] == "error"
    assert "no model is configured" in events[-1]["detail"]


async def test_tool_results_are_paired_with_the_assistant_turn_that_asked(
    db_available: None,
) -> None:
    """Every role:"tool" message must follow an assistant message carrying the matching
    tool_calls. Providers that validate the pairing reject the whole request otherwise --
    seen live as "Messages with role 'tool' must be a response to a preceding message
    with 'tool_calls'", which killed the assistant the moment it used any tool.
    """
    tenant_id, workspace_id, viewer = await _setup("chat-pairing")
    provider = _ScriptedChatProvider(
        turns=[
            ("", (ToolCall(id="c1", name="list_personas", arguments={}),)),
            ("You have an assistant persona.", ()),
        ]
    )

    await _collect(
        chat(
            tenant_id,
            workspace_id,
            viewer,
            [{"role": "user", "content": "what personas exist?"}],
            embedder=StubEmbeddingProvider(dimension=1024),
            provider_factory=lambda _p: provider,
            encryptor=IdentityEncryptor(),
            permission_service=RolePermissionService(),
        )
    )

    # The SECOND request is the one that carries the tool result back.
    assert provider.calls == 2
    messages = provider.seen_messages[1]
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs, "the tool result never reached the model"

    for tool_msg in tool_msgs:
        idx = messages.index(tool_msg)
        # Walk back to the nearest non-tool message: it must be the assistant turn that
        # requested this call, and it must name the call id.
        preceding = next(m for m in reversed(messages[:idx]) if m.get("role") != "tool")
        assert preceding["role"] == "assistant", preceding["role"]
        ids = {c["id"] for c in preceding.get("tool_calls") or []}
        assert tool_msg["tool_call_id"] in ids, (
            f"tool_call_id {tool_msg['tool_call_id']!r} not in preceding assistant "
            f"tool_calls {ids!r}"
        )
        # OpenAI shape, as the providers expect it.
        call = next(c for c in preceding["tool_calls"] if c["id"] == tool_msg["tool_call_id"])
        assert call["type"] == "function"
        assert call["function"]["name"] == "list_personas"
        json.loads(call["function"]["arguments"])  # arguments travel as a JSON string


async def test_docs_tools_are_offered_and_answer(db_available: None) -> None:
    """The manual is inside the assistant: search_docs is offered on every turn, the
    system prompt names the pages, and a search lands on the right page without a model."""
    tenant_id, workspace_id, viewer = await _setup("chat-docs")
    provider = _ScriptedChatProvider(
        turns=[
            ("", (ToolCall(id="c1", name="search_docs", arguments={"query": "secret_mode"}),)),
            ("secret_mode is a workspace setting.", ()),
        ]
    )

    events = await _collect(
        chat(
            tenant_id,
            workspace_id,
            viewer,
            [{"role": "user", "content": "what does secret_mode do?"}],
            embedder=StubEmbeddingProvider(dimension=1024),
            provider_factory=lambda _p: provider,
            encryptor=IdentityEncryptor(),
            permission_service=RolePermissionService(),
        )
    )

    assert {"type": "tool", "name": "search_docs"} in events
    assert events[-1]["type"] == "done"
    assert "search_docs" in provider.seen_tools[0] and "read_doc" in provider.seen_tools[0]
    assert "Product documentation pages" in provider.seen_messages[0][0]["content"]
    tool_reply = provider.seen_messages[1][-1]["content"]
    assert '"page": "configuration"' in tool_reply


async def _chat_events(tenant_id, workspace_id, viewer, provider, question: str) -> list[dict]:  # noqa: ANN001
    return await _collect(
        chat(
            tenant_id,
            workspace_id,
            viewer,
            [{"role": "user", "content": question}],
            embedder=StubEmbeddingProvider(dimension=1024),
            provider_factory=lambda _p: provider,
            encryptor=IdentityEncryptor(),
            permission_service=RolePermissionService(),
        )
    )


async def _outsider(tenant_id):  # noqa: ANN001, ANN202
    """A tenant editor with no workspace membership: may use the tenant, sees no workspace."""
    from core.tenancy.models import Principal
    from core.tenancy.provisioning import create_tenant_user
    from core.tenancy.scope import tenant_scope

    pid = await create_tenant_user(tenant_id, "Outsider", "editor")
    async with tenant_scope(tenant_id) as session:
        principal = await session.get(Principal, pid)
        assert principal is not None
        session.expunge(principal)
    return principal


async def _make_steward(tenant_id, workspace_id, principal_id) -> None:  # noqa: ANN001
    """The seeded owner's workspace role does not author secrets; a steward's does."""
    from sqlalchemy import select

    from core.tenancy.models import WorkspaceMembership
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.principal_id == principal_id,
            )
        )
        if row is None:
            session.add(
                WorkspaceMembership(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    principal_id=principal_id,
                    role="steward",
                )
            )
        else:
            row.role = "steward"


async def test_list_secrets_shows_gists_never_content(db_available: None) -> None:
    """INV-8 at the assistant: an author asking for the secrets gets the gists; the
    plaintext the author could read in the UI still never enters the model's context."""
    from adapters.moderation.allow_all import AllowAllModerationProvider
    from core.secrets.authoring import create_secret

    tenant_id, workspace_id, viewer = await _setup("chat-secrets")
    await create_secret(
        tenant_id,
        workspace_id,
        viewer.id,
        subject_kind="entity",
        subject_id=uuid.uuid4(),
        content="the butler did it",
        gist="knows who did it",
        scope_key="workspace_public",
        encryptor=IdentityEncryptor(),
        permission_service=RolePermissionService(),
        moderation_provider=AllowAllModerationProvider(),
    )
    provider = _ScriptedChatProvider(
        turns=[("", (ToolCall(id="c1", name="list_secrets", arguments={}),)), ("One secret.", ())]
    )
    events = await _chat_events(tenant_id, workspace_id, viewer, provider, "what secrets exist?")
    assert events[-1]["type"] == "done"
    tool_reply = provider.seen_messages[1][-1]["content"]
    assert "knows who did it" in tool_reply
    assert "butler" not in tool_reply
    assert "content" not in json.loads(tool_reply)[0]


async def test_gated_reads_refuse_a_non_member(db_available: None) -> None:
    """The read tools answer under the asking user's own permissions: someone who is in
    the organization but not in the workspace gets a refusal, not the workspace."""
    tenant_id, workspace_id, _owner = await _setup("chat-outsider")
    outsider = await _outsider(tenant_id)
    for tool, args in (
        ("list_secrets", {}),
        ("get_workspace", {}),
        ("list_entities", {}),
        ("list_personas", {}),
        ("list_process_definitions", {}),
        ("get_session", {"session_id": str(uuid.uuid4())}),
    ):
        provider = _ScriptedChatProvider(
            turns=[("", (ToolCall(id="c1", name=tool, arguments=args),)), ("Sorry.", ())]
        )
        events = await _chat_events(tenant_id, workspace_id, outsider, provider, "show me")
        assert events[-1]["type"] == "done", tool
        assert "proposal" not in {e["type"] for e in events}
        reply = json.loads(provider.seen_messages[1][-1]["content"])
        assert reply == {"error": "not permitted: view_workspace"}, tool


async def test_get_workspace_lists_members_with_principal_ids(db_available: None) -> None:
    tenant_id, workspace_id, viewer = await _setup("chat-workspace")
    provider = _ScriptedChatProvider(
        turns=[("", (ToolCall(id="c1", name="get_workspace", arguments={}),)), ("Here.", ())]
    )
    await _chat_events(tenant_id, workspace_id, viewer, provider, "who is here?")
    reply = json.loads(provider.seen_messages[1][-1]["content"])
    assert "id" in reply, reply
    assert reply["id"] == str(workspace_id)
    assert any(m["principal_id"] == str(viewer.id) for m in reply["members"])
    assert "settings" in reply and "clock_value" in reply


async def test_secret_and_schema_tools_propose_without_writing(db_available: None) -> None:
    """Two write tools in one turn -> two proposals, nothing written; a field the tool
    never declared (a token the model invented) is dropped before the proposal is made,
    and the secret's plaintext is hidden in the summary the model reads back."""
    from core.entities.repo import list_latest_schemas
    from core.secrets.authoring import list_secret_views_for_workspace

    tenant_id, workspace_id, viewer = await _setup("chat-writes")
    provider = _ScriptedChatProvider(
        turns=[
            (
                "",
                (
                    ToolCall(
                        id="c1",
                        name="create_secret",
                        arguments={
                            "subject_kind": "entity",
                            "subject_id": str(uuid.uuid4()),
                            "content": "the butler did it",
                            "gist": "knows who did it",
                            "scope_key": "workspace_public",
                            "access_token": "ghp_should_not_travel",
                        },
                    ),
                    ToolCall(
                        id="c2",
                        name="create_entity_schema",
                        arguments={"key": "clue", "definition": {"fields": []}},
                    ),
                ),
            ),
            ("Proposed both.", ()),
        ]
    )
    events = await _chat_events(tenant_id, workspace_id, viewer, provider, "do it")
    proposals = [e for e in events if e["type"] == "proposal"]
    assert [p["action"] for p in proposals] == ["create_secret", "create_entity_schema"]
    assert "access_token" not in proposals[0]["args"]
    assert "butler" not in proposals[0]["summary"] and "(hidden)" in proposals[0]["summary"]
    assert proposals[0]["args"]["content"] == "the butler did it"  # travels to Apply only
    assert (
        await list_secret_views_for_workspace(
            tenant_id,
            workspace_id,
            viewer.id,
            encryptor=IdentityEncryptor(),
            permission_service=RolePermissionService(),
        )
        == []
    )
    assert not [r for r in await list_latest_schemas(tenant_id, workspace_id) if r.key == "clue"]
