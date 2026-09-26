"""The assistant chat: a session-less, streaming, tool-calling conversation.

The floating widget's backend. Unlike a live-session turn (``run_agent_turn``), nothing
here persists a transcript -- history lives in the caller's browser and arrives with
every request. The assistant can *read* workspace data through viewer-safe service
calls, and can *propose* any edit the UI offers -- but it never executes a write:
a write tool records a ``ProposedAction`` that streams to the UI as a card, and the
user's **Apply** click calls the ordinary API endpoint from their own authenticated
browser session. The assistant's effective access is therefore exactly the asking
user's, by construction, with no new permission surface (and no new INV-5/INV-8
exposure: reads go through the same services the UI uses).

Streaming: ``chat()`` is an async iterator of small event dicts (text deltas, tool
markers, proposals, done/error) that the API layer writes out as NDJSON -- the fix for
"I asked and it never responded": the first token arrives as soon as the model produces
it, and an overall ``asyncio.timeout`` turns a wedged provider into a visible error
event instead of an eternally pending request.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import structlog

from core.agents.assistant import (
    _CONTEXT_MAX_TOKENS_SETTING,
    _DEFAULT_CONTEXT_MAX_TOKENS,
    _class_ratios,
    _workspace_context,
    ensure_workspace_assistant,
)
from core.agents.authoring import (
    list_agents,
    list_personas,
    resolve_connection_api_key,
)
from core.agents.models import Agent
from core.agents.tools import ToolContext, ToolRegistry, ToolResult
from core.audit.models import UsageRecordRow
from core.knowledge.authoring import list_draft_entries, list_sources
from core.ports.embedding import EmbeddingProvider
from core.ports.encryptor import Encryptor
from core.ports.model_provider import (
    GenerationRequest,
    ModelProvider,
    ToolCall,
    ToolSpec,
)
from core.repos.service import list_repos
from core.sessions.lifecycle import list_sessions
from core.settings.resolve import resolved_setting
from core.tenancy.egress import load_egress_policy
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope
from core.workflows.service import list_workflows_for_tenant

log = structlog.get_logger()

_MAX_TOOL_ITERATIONS = 6
_CHAT_TIMEOUT_S = 300
_MAX_HISTORY_MESSAGES = 30

_CHAT_SYSTEM = (
    "You are chatting with a human running this workspace. Answer questions using the "
    "workspace knowledge and the read tools. When the user asks you to change "
    "something, call the matching write tool with complete arguments -- the change is "
    "then SHOWN TO THE USER FOR CONFIRMATION, never applied by you directly; tell the "
    "user you have proposed it and what it contains. Use tools only when they help; "
    "answer directly otherwise. Be concise."
)


@dataclass
class ProposedAction:
    action: str
    args: dict[str, Any]
    summary: str


@dataclass
class _ChatState:
    proposals: list[ProposedAction] = field(default_factory=list)


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


def _s(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


def _register_read_tools(
    registry: ToolRegistry, tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> None:
    """Read tools execute inline: they call the same core services the UI's GET routes
    use, so they can never show the assistant more than the UI would show the user."""

    async def _list_personas(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        rows = await list_personas(tenant_id, workspace_id)
        return ToolResult(
            content=json.dumps(
                [
                    {
                        "id": str(r.id),
                        "key": r.key,
                        "name": r.name,
                        "type": r.persona_type,
                        "persona_md": r.persona_md[:400],
                    }
                    for r in rows
                ]
            )
        )

    async def _list_profiles(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        rows = await list_agents(tenant_id)
        return ToolResult(
            content=json.dumps(
                [
                    {"id": str(r.id), "name": r.name, "provider": r.provider, "model": r.model}
                    for r in rows
                ]
            )
        )

    async def _list_sources(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        rows = await list_sources(tenant_id)
        return ToolResult(
            content=json.dumps(
                [{"id": str(r.id), "key": r.key, "name": r.name, "class": r.class_} for r in rows]
            )
        )

    async def _list_entries(args: dict[str, object], _c: ToolContext) -> ToolResult:
        source_key = str(args.get("source_key", ""))
        source = next((s for s in await list_sources(tenant_id) if s.key == source_key), None)
        if source is None:
            return ToolResult(content=f"no knowledge source with key {source_key!r}")
        rows = await list_draft_entries(tenant_id, source.id)
        return ToolResult(
            content=json.dumps(
                [
                    {
                        "entry_key": r.entry_key,
                        "title": r.title,
                        "class": r.class_,
                        "body_md": r.body_md[:600],
                    }
                    for r in rows
                ]
            )
        )

    async def _list_sessions(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        rows = await list_sessions(tenant_id, workspace_id)
        return ToolResult(
            content=json.dumps(
                [
                    {
                        "id": str(r.id),
                        "name": r.name,
                        "status": r.status,
                        "phase": r.current_phase,
                        "turn_policy": r.turn_policy,
                        "agenda_md": (r.agenda_md or "")[:200],
                    }
                    for r in rows[:20]
                ]
            )
        )

    async def _list_repos(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        rows = await list_repos(tenant_id)
        return ToolResult(
            content=json.dumps(
                [
                    {
                        "id": str(r.id),
                        "key": r.key,
                        "name": r.name,
                        "runtime": r.runtime,
                        "source_url": r.source_url,
                    }
                    for r in rows
                ]
            )
        )

    async def _list_workflows(_a: dict[str, object], _c: ToolContext) -> ToolResult:
        rows = await list_workflows_for_tenant(tenant_id)
        return ToolResult(
            content=json.dumps(
                [{"key": r.key, "name": r.name, "is_system": r.tenant_id is None} for r in rows]
            )
        )

    registry.register(
        ToolSpec(
            name="list_personas",
            description="List this workspace's agent personas.",
            parameters=_obj({}, []),
        ),
        _list_personas,
    )
    registry.register(
        ToolSpec(
            name="list_model_profiles",
            description="List the tenant's model connections (provider/model).",
            parameters=_obj({}, []),
        ),
        _list_profiles,
    )
    registry.register(
        ToolSpec(
            name="list_knowledge_sources",
            description="List knowledge sources (rulebooks, handbooks, briefs).",
            parameters=_obj({}, []),
        ),
        _list_sources,
    )
    registry.register(
        ToolSpec(
            name="list_knowledge_entries",
            description="List the entries of one knowledge source by its key.",
            parameters=_obj({"source_key": _s("the knowledge source's key")}, ["source_key"]),
        ),
        _list_entries,
    )
    registry.register(
        ToolSpec(
            name="list_sessions",
            description="List this workspace's sessions.",
            parameters=_obj({}, []),
        ),
        _list_sessions,
    )
    registry.register(
        ToolSpec(
            name="list_repos",
            description="List the registered code repositories.",
            parameters=_obj({}, []),
        ),
        _list_repos,
    )
    registry.register(
        ToolSpec(
            name="list_workflows", description="List available workflows.", parameters=_obj({}, [])
        ),
        _list_workflows,
    )


# One entry per write tool: (description, JSON-schema params). Args mirror the request
# bodies of the API endpoints the UI's Apply step calls -- the frontend's action map is
# the executable counterpart of this table.
_WRITE_TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    "update_persona": (
        "Update an existing persona (name, persona_md prose, persona_type, web_search).",
        _obj(
            {
                "persona_id": _s("the persona's id (from list_personas)"),
                "name": _s("new display name (optional)"),
                "persona_md": _s("full replacement persona prose (optional)"),
                "persona_type": _s("supervisor | participant | informational (optional)"),
            },
            ["persona_id"],
        ),
    ),
    "create_persona": (
        "Create a new persona in this workspace.",
        _obj(
            {
                "key": _s("stable slug"),
                "name": _s("display name"),
                "persona_md": _s("persona prose"),
                "persona_type": _s("supervisor | participant"),
                "agent_id": _s("model connection id (from list_model_profiles)"),
            },
            ["key", "name", "agent_id"],
        ),
    ),
    "archive_persona": (
        "Archive (soft-delete) a persona.",
        _obj({"persona_id": _s("the persona's id")}, ["persona_id"]),
    ),
    "upsert_knowledge_entry": (
        "Create or replace one entry in a knowledge source.",
        _obj(
            {
                "source_key": _s("knowledge source key (from list_knowledge_sources)"),
                "entry_key": _s("entry key (new or existing)"),
                "title": _s("entry title"),
                "body_md": _s("full markdown body"),
                "class": _s("rules | lore | misc"),
            },
            ["source_key", "entry_key", "title", "body_md"],
        ),
    ),
    "create_knowledge_source": (
        "Create a new knowledge source (rulebook/handbook).",
        _obj(
            {
                "key": _s("stable slug"),
                "name": _s("display name"),
                "class": _s("rules | lore | misc"),
            },
            ["key", "name"],
        ),
    ),
    "publish_knowledge_source": (
        "Publish a knowledge source's current draft entries as a new version.",
        _obj(
            {"source_key": _s("knowledge source key"), "change_note": _s("what changed")},
            ["source_key"],
        ),
    ),
    "rename_session": (
        "Rename a session.",
        _obj(
            {"session_id": _s("session id (from list_sessions)"), "name": _s("new display name")},
            ["session_id", "name"],
        ),
    ),
    "set_session_agenda": (
        "Set or replace a session's agenda.",
        _obj(
            {"session_id": _s("session id"), "agenda_md": _s("full agenda markdown")},
            ["session_id", "agenda_md"],
        ),
    ),
    "set_turn_policy": (
        "Switch a session between autonomous and managed turn policy.",
        _obj(
            {"session_id": _s("session id"), "turn_policy": _s("auto | directed")},
            ["session_id", "turn_policy"],
        ),
    ),
    "update_workflow": (
        "Update a tenant-authored workflow (name, label overrides, featured flows).",
        _obj(
            {
                "key": _s("workflow key (from list_workflows; not a system one)"),
                "name": _s("new name (optional)"),
            },
            ["key"],
        ),
    ),
    "set_current_workflow": (
        "Switch the tenant's active workflow.",
        _obj({"workflow_key": _s("workflow key")}, ["workflow_key"]),
    ),
    "update_repo": (
        "Update a registered repository (name, remote URL, runtime, setup/test commands).",
        _obj(
            {
                "repo_id": _s("repo id (from list_repos)"),
                "name": _s("new name (optional)"),
                "source_url": _s("remote https URL (optional)"),
                "runtime": _s("debian | node20 | python312 | java21 (optional)"),
                "test_cmd": _s("test command (optional)"),
            },
            ["repo_id"],
        ),
    ),
    "create_model_profile": (
        "Create a model connection.",
        _obj(
            {
                "name": _s("display name"),
                "provider": _s("ollama | openai | anthropic | gemini"),
                "model": _s("model name"),
                "api_base": _s("endpoint URL (optional)"),
            },
            ["name", "provider", "model"],
        ),
    ),
    "update_model_profile": (
        "Update a model connection (never its stored key).",
        _obj(
            {
                "profile_id": _s("connection id (from list_model_profiles)"),
                "name": _s("new name (optional)"),
                "model": _s("new model (optional)"),
                "api_base": _s("new endpoint URL (optional)"),
            },
            ["profile_id"],
        ),
    ),
}


def _register_write_tools(registry: ToolRegistry, state: _ChatState) -> None:
    """Write tools NEVER execute: the handler records a proposal for the UI. The model
    is told so in its result, so it reports the proposal instead of claiming success."""
    for action, (description, parameters) in _WRITE_TOOLS.items():

        async def _propose(
            args: dict[str, object], _c: ToolContext, *, _action: str = action
        ) -> ToolResult:
            summary_bits = ", ".join(
                f"{k}={str(v)[:60]}" for k, v in args.items() if v not in (None, "")
            )
            state.proposals.append(
                ProposedAction(
                    action=_action,
                    args={k: v for k, v in args.items() if v not in (None, "")},
                    summary=f"{_action}({summary_bits})",
                )
            )
            return ToolResult(
                content=(
                    f"Proposed '{_action}' to the user -- it is shown in the chat with "
                    "an Apply button and takes effect only if they confirm. Tell the "
                    "user what you proposed."
                )
            )

        registry.register(
            ToolSpec(
                name=action,
                description=f"{description} PROPOSAL ONLY: the user confirms in the UI.",
                parameters=parameters,
            ),
            _propose,
        )


async def chat(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    viewer: Principal,
    messages: list[dict[str, str]],
    *,
    embedder: EmbeddingProvider,
    provider_factory: Any,  # Callable[[str], ModelProvider] -- composition root's
    encryptor: Encryptor,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one assistant reply (plus any proposals) for the given history."""
    try:
        async with asyncio.timeout(_CHAT_TIMEOUT_S):
            async for event in _chat_inner(
                tenant_id,
                workspace_id,
                viewer,
                messages,
                embedder=embedder,
                provider_factory=provider_factory,
                encryptor=encryptor,
            ):
                yield event
    except TimeoutError:
        yield {"type": "error", "detail": f"assistant timed out after {_CHAT_TIMEOUT_S}s"}
    except Exception as exc:  # noqa: BLE001 -- the stream is the error channel
        log.warning("assistant_chat.failed", error=str(exc))
        yield {"type": "error", "detail": str(exc)[:300]}


async def _chat_inner(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    viewer: Principal,
    messages: list[dict[str, str]],
    *,
    embedder: EmbeddingProvider,
    provider_factory: Any,
    encryptor: Encryptor,
) -> AsyncIterator[dict[str, Any]]:
    persona = await ensure_workspace_assistant(tenant_id, workspace_id)
    async with tenant_scope(tenant_id) as session:
        profile = await session.get(Agent, persona.agent_id)
        if profile is None:
            raise ValueError("the assistant's model profile is missing")
        if not profile.model:
            # Fresh install, no provider configured yet -- name the fix rather than
            # letting the stream die on a connection error to a host nobody set up.
            raise ValueError(
                "no model is configured for the assistant yet -- set one on the "
                "'Assistant model' profile under Personas"
            )
        session.expunge(profile)
    from core.usage_limits import ensure_within_limits

    await ensure_within_limits(
        tenant_id, agent_id=profile.id, persona_id=persona.id, principal_id=viewer.id
    )
    provider: ModelProvider = provider_factory(profile.provider)
    api_key = await resolve_connection_api_key(
        tenant_id, profile.credential_ref, encryptor=encryptor
    )

    history: list[dict[str, object]] = [
        {"role": m["role"], "content": m["content"]}
        for m in messages[-_MAX_HISTORY_MESSAGES:]
        if m.get("role") in ("user", "assistant", "system") and m.get("content")
    ]
    last_user = str(next((m["content"] for m in reversed(history) if m["role"] == "user"), ""))
    # Same budget the one-shot assist path resolves: the chat widget and /assist are the
    # same assistant reading the same workspace, and a budget that applied to one of them
    # would be a setting the user could only half-see the effect of.
    context, entry_keys = await _workspace_context(
        tenant_id,
        workspace_id,
        viewer,
        last_user,
        embedder,
        int(
            await resolved_setting(
                tenant_id,
                workspace_id,
                _CONTEXT_MAX_TOKENS_SETTING,
                _DEFAULT_CONTEXT_MAX_TOKENS,
            )
        ),
        await _class_ratios(tenant_id, workspace_id),
    )

    state = _ChatState()
    registry = ToolRegistry()
    _register_read_tools(registry, tenant_id, workspace_id)
    _register_write_tools(registry, state)

    system = f"{persona.persona_md}\n\n{_CHAT_SYSTEM}"
    if context:
        system += f"\n\nWorkspace knowledge (viewer-scoped):\n{context}"
    conversation: list[dict[str, object]] = [
        {"role": "system", "content": system},
        *history,
    ]

    model_string = f"{profile.provider}/{profile.model}"
    tool_ctx = ToolContext(tenant_id=tenant_id, persona_id=persona.id, session_id=None)

    for _iteration in range(_MAX_TOOL_ITERATIONS):
        req = GenerationRequest(
            egress_policy=await load_egress_policy(tenant_id),
            model=model_string,
            messages=conversation,
            purpose="generation",
            max_tokens=900,
            tools=registry.specs(),
            api_base=profile.api_base,
            params=dict(profile.params or {}),
            api_key=api_key,
        )
        pieces: list[str] = []
        tool_calls: tuple[ToolCall, ...] = ()
        start = time.monotonic()
        async for chunk in provider.generate(req):
            if chunk.text:
                pieces.append(chunk.text)
                yield {"type": "text", "delta": chunk.text}
            if chunk.tool_calls:
                tool_calls = chunk.tool_calls
        content = "".join(pieces)

        async with tenant_scope(tenant_id) as session:
            session.add(
                UsageRecordRow(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    persona_id=persona.id,
                    agent_id=profile.id,
                    provider=profile.provider,
                    model=profile.model,
                    purpose="generation",
                    principal_id=viewer.id,
                    prompt_tokens=sum(
                        provider.count_tokens(str(m.get("content") or ""), model_string)
                        for m in conversation
                    ),
                    completion_tokens=provider.count_tokens(content, model_string),
                    latency_ms=int((time.monotonic() - start) * 1000),
                )
            )

        if not tool_calls:
            yield {"type": "done", "context_entry_keys": entry_keys}
            return

        # The assistant turn that REQUESTED the tools must carry tool_calls back in the
        # transcript, in the OpenAI shape -- the same requirement core/agents/runtime.py
        # already honours for session turns. Without it every provider that validates the
        # pairing rejects the NEXT request outright ("Messages with role 'tool' must be a
        # response to a preceding message with 'tool_calls'"), so the assistant could call
        # one tool and then die instead of answering.
        conversation.append(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in tool_calls
                ],
            }
        )
        for tool_call in tool_calls:
            yield {"type": "tool", "name": tool_call.name}
            before = len(state.proposals)
            try:
                result = await registry.dispatch(tool_call, tool_ctx)
                result_content = result.content
            except Exception as exc:  # noqa: BLE001 -- feed the failure back to the model
                result_content = f"tool {tool_call.name} failed: {str(exc)[:300]}"
            for proposal in state.proposals[before:]:
                yield {
                    "type": "proposal",
                    "action": proposal.action,
                    "args": proposal.args,
                    "summary": proposal.summary,
                }
            conversation.append(
                {
                    "role": "tool",
                    "content": result_content,
                    "tool_call_id": tool_call.id,
                }
            )

    yield {"type": "error", "detail": f"tool loop exceeded {_MAX_TOOL_ITERATIONS} iterations"}
