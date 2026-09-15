"""An assistant for the platform-admin console.

The admin console has no workspaces and therefore no personas, so the floating workspace
assistant has nothing to attach to -- which left the one screen where a mistake is
deployment-wide as the one screen with no help on it.

This is the smaller sibling of ``core.agents.assistant_chat``, and deliberately so. No
persona, no knowledge retrieval, no workspace context: an operator asking "why is search
slow" or "how do I point this at a different embedding model" wants the deployment's own
state and a concrete next step, not a character.

**It proposes; it never applies.** Exactly the tenant assistant's contract: a write tool
records a proposal that streams to the UI as a card, and the operator's *Apply* click
calls the ordinary admin endpoint from their own authenticated browser session. The
assistant therefore holds no privilege of its own -- it cannot change a deployment any
more than the person reading it can, and everything it does lands in the same audit trail
as if they had clicked it themselves. Given the blast radius of this particular screen,
that is not a nicety; it is the reason this can exist at all.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import structlog

from core.agents.tools import ToolContext, ToolRegistry, ToolResult
from core.ports.model_provider import GenerationRequest, ToolSpec

log = structlog.get_logger()

_TIMEOUT_S = 120
_MAX_TOOL_ITERATIONS = 6

_SYSTEM = (
    "You are the assistant on the platform-admin console of a Pyrrhula deployment. You "
    "are talking to whoever operates this installation.\n\n"
    "Use the read tools to answer from what this deployment actually reports, never from "
    "what a default installation would look like -- an operator asking about their box "
    "wants their box.\n\n"
    "When they ask for a change, call the matching propose tool with complete arguments. "
    "That SHOWS the change to them with an Apply button; you never apply anything "
    "yourself. Say what you proposed and what it will do. If a change carries a "
    "consequence they may not have considered -- switching the embedding model orphans "
    "every existing vector, for instance -- say so before they click.\n\n"
    "Be concise and concrete. Prefer the exact setting name and value over prose."
)


@dataclass
class ProposedAction:
    action: str
    args: dict[str, Any]
    summary: str


@dataclass
class _State:
    proposals: list[ProposedAction] = field(default_factory=list)


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


async def _deployment_state() -> dict[str, Any]:
    """What this deployment currently is, as one read.

    Kept to things an operator asks about and that are cheap: model cache, pack sources,
    tenants. Nothing here is tenant data -- the admin console is deployment scope, and an
    assistant that could read into tenants from here would be a new way through tenancy.
    """
    from core.deployment_settings import get_retrieval_models
    from core.plugins.service import list_repositories
    from core.retrieval_cache import cache_status
    from core.tenancy.provisioning import list_tenants

    models = await get_retrieval_models()
    cache = await cache_status()
    repos = await list_repositories()
    tenants = await list_tenants()
    return {
        "retrieval_models": models,
        "model_cache": cache,
        "plugin_repositories": [{"url": r.url, "ref": r.ref, "enabled": r.enabled} for r in repos],
        "tenants": [{"slug": t.slug, "name": t.name} for t in tenants][:50],
        "tenant_count": len(tenants),
    }


def _register_tools(registry: ToolRegistry, state: _State) -> list[ToolSpec]:
    specs: list[ToolSpec] = []

    async def _read(_args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
        return ToolResult(content=json.dumps(await _deployment_state(), default=str))

    spec = ToolSpec(
        name="deployment_status",
        description=(
            "This deployment's current state: configured retrieval models, whether their "
            "files are on disk and how big, plugin repositories, and the tenant list."
        ),
        parameters=_obj({}, []),
    )
    registry.register(spec, _read)
    specs.append(spec)

    def _proposer(action: str):  # noqa: ANN202
        async def handler(args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
            supplied = {k: v for k, v in args.items() if v not in (None, "")}
            bits = ", ".join(f"{k}={str(v)[:60]}" for k, v in supplied.items())
            state.proposals.append(
                ProposedAction(action=action, args=supplied, summary=f"{action}({bits})")
            )
            return ToolResult(
                content=(
                    f"Proposed '{action}'. It is shown to the operator with an Apply "
                    "button and takes effect only if they click it. Tell them what you "
                    "proposed and any consequence they should weigh first."
                )
            )

        return handler

    writes: list[tuple[str, str, dict[str, Any]]] = [
        (
            "set_retrieval_models",
            "Propose changing the embedding and/or reranker model. Changing the embedding "
            "model orphans every existing vector (embeddings from different models are "
            "not comparable) and that content needs re-indexing -- warn about this.",
            _obj(
                {
                    "embedding_model": {"type": "string"},
                    "embedding_dimension": {"type": "integer"},
                    "reranker_model": {"type": "string"},
                    "reranker_enabled": {"type": "boolean"},
                },
                ["embedding_model", "embedding_dimension"],
            ),
        ),
        (
            "download_retrieval_models",
            "Propose fetching the configured retrieval models from Hugging Face into this "
            "deployment's shared cache. Use when the status shows a model is not present.",
            _obj({}, []),
        ),
        (
            "add_plugin_repository",
            "Propose registering a workflow-plugin repository, pinned to a ref. All three "
            "fields are required by the endpoint that applies this.",
            _obj(
                {
                    "name": {"type": "string"},
                    "url": {"type": "string"},
                    "ref": {"type": "string"},
                },
                ["name", "url", "ref"],
            ),
        ),
    ]
    for action, description, parameters in writes:
        spec = ToolSpec(name=action, description=description, parameters=parameters)
        registry.register(spec, _proposer(action))
        specs.append(spec)
    return specs


async def admin_chat(
    messages: list[dict[str, str]],
    *,
    provider_factory: Any,
    connection: Any,
    api_key: str | None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one reply, as the same NDJSON events the workspace widget already speaks."""
    try:
        async with asyncio.timeout(_TIMEOUT_S):
            async for event in _inner(messages, provider_factory, connection, api_key):
                yield event
    except TimeoutError:
        yield {"type": "error", "detail": f"assistant timed out after {_TIMEOUT_S}s"}
    except Exception as exc:  # noqa: BLE001 -- surface it as an event, never a 500 mid-stream
        log.warning("admin_assistant.failed", error=str(exc)[:300])
        yield {"type": "error", "detail": str(exc)[:300]}


async def _inner(
    messages: list[dict[str, str]],
    provider_factory: Any,
    connection: Any,
    api_key: str | None,
) -> AsyncIterator[dict[str, Any]]:
    state = _State()
    registry = ToolRegistry()
    specs = _register_tools(registry, state)

    provider = provider_factory(connection.provider)
    model = f"{connection.provider}/{connection.model}"
    conversation: list[dict[str, Any]] = [{"role": "system", "content": _SYSTEM}]
    conversation += [{"role": m["role"], "content": m["content"]} for m in messages]

    from core.tenancy.admin import ADMIN_TENANT_ID

    # The reserved admin tenant, and a nil persona: these tools read deployment state and
    # record proposals, so neither field is consulted -- but ToolContext requires them and
    # inventing a real persona id here would be a lie about who acted.
    ctx = ToolContext(
        tenant_id=ADMIN_TENANT_ID,
        persona_id=uuid.UUID(int=0),
        session_id=None,
    )

    for _ in range(_MAX_TOOL_ITERATIONS):
        request = GenerationRequest(
            model=model,
            messages=conversation,
            purpose="generation",
            tools=tuple(specs),
            api_base=connection.api_base,
            api_key=api_key,
            params=dict(connection.params or {}),
        )
        text_parts: list[str] = []
        tool_calls: list[Any] = []
        async for chunk in provider.generate(request):
            if chunk.text:
                text_parts.append(chunk.text)
                yield {"type": "text", "delta": chunk.text}
            if chunk.tool_calls:
                tool_calls = list(chunk.tool_calls)

        if not tool_calls:
            yield {
                "type": "done",
                "proposals": [
                    {"action": p.action, "args": p.args, "summary": p.summary}
                    for p in state.proposals
                ],
            }
            return

        conversation.append(
            {
                "role": "assistant",
                "content": "".join(text_parts),
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in tool_calls
                ],
            }
        )
        for call in tool_calls:
            yield {"type": "tool", "name": call.name}
            result = await registry.dispatch(call, ctx)
            conversation.append(
                {"role": "tool", "tool_call_id": call.id, "content": result.content}
            )

    yield {"type": "error", "detail": f"tool loop exceeded {_MAX_TOOL_ITERATIONS} iterations"}


ADMIN_ASSISTANT_CONNECTION = "Admin assistant model"


async def get_admin_connection() -> Any | None:
    """The model connection the admin assistant runs on, or None if none is configured.

    Deliberately a connection on the reserved admin tenant rather than a new deployment
    setting: it reuses the encrypted-credential storage, the egress policy and the
    provider selection the rest of the product already has, instead of inventing a
    second, thinner way to hold a provider key.
    """
    from sqlalchemy import select

    from core.agents.models import Agent
    from core.tenancy.admin import ADMIN_TENANT_ID
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(ADMIN_TENANT_ID) as session:
        row = await session.scalar(
            select(Agent).where(
                Agent.tenant_id == ADMIN_TENANT_ID,
                Agent.name == ADMIN_ASSISTANT_CONNECTION,
                Agent.archived_at.is_(None),
            )
        )
        if row is None or not row.provider or not row.model:
            return None
        session.expunge(row)
        return row


async def set_admin_connection(
    *, provider: str, model: str, api_base: str | None, api_key: str | None, encryptor: Any
) -> Any:
    """Create or update it. An omitted key keeps the stored one, so an operator editing the
    model does not have to re-paste a credential they cannot read back."""
    from sqlalchemy import select

    from core.agents.authoring import create_agent, update_agent
    from core.agents.models import Agent
    from core.tenancy.admin import ADMIN_TENANT_ID
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(ADMIN_TENANT_ID) as session:
        existing = await session.scalar(
            select(Agent).where(
                Agent.tenant_id == ADMIN_TENANT_ID,
                Agent.name == ADMIN_ASSISTANT_CONNECTION,
                Agent.archived_at.is_(None),
            )
        )
        existing_id = existing.id if existing else None

    if existing_id is None:
        return await create_agent(
            ADMIN_TENANT_ID,
            ADMIN_ASSISTANT_CONNECTION,
            provider,
            model,
            api_key=api_key,
            api_base=api_base,
            encryptor=encryptor,
        )
    return await update_agent(
        ADMIN_TENANT_ID,
        existing_id,
        provider=provider,
        model=model,
        api_base=api_base,
        api_key=api_key or None,
        encryptor=encryptor,
    )
