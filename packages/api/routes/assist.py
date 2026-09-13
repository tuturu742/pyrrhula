"""The workspace assistant's API surface: fetch (ensuring) the required assistant persona
and ask it for internal-task help (draft a persona description, draft a knowledge entry,
answer a question about the workspace). Repo analysis (the software-flow knowledge graph)
is enqueued from here too -- the analysis itself is a worker job.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from api.embedding_provider_factory import get_embedding_provider
from api.encryptor_factory import get_encryptor
from api.job_queue_factory import get_job_queue
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from api.model_provider_factory import get_model_provider
from core.agents.assistant import (
    UnknownAssistTaskError,
    assist,
    ensure_workspace_assistant,
)
from core.agents.authoring import get_agent, resolve_connection_api_key
from core.agents.models import Persona
from core.ports.model_provider import EgressDeniedError
from core.repos.service import list_repos
from core.tenancy.context import RequestContext
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope

_ASSIST_TIMEOUT_S = 240

router = APIRouter(
    prefix="/workspaces",
    tags=["assist"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


class AssistantResponse(BaseModel):
    persona_id: uuid.UUID
    key: str
    name: str
    persona_type: str
    persona_md: str
    agent_id: uuid.UUID


def _assistant_response(row: Persona) -> AssistantResponse:
    return AssistantResponse(
        persona_id=row.id,
        key=row.key,
        name=row.name,
        persona_type=row.persona_type,
        persona_md=row.persona_md,
        agent_id=row.agent_id,
    )


@router.get("/{workspace_id}/assistant")
async def get_assistant_endpoint(
    workspace_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> AssistantResponse:
    """Ensures on read: the assistant is *required*, so the first workspace surface that
    asks for it is also what brings it into existence (idempotent)."""
    persona = await ensure_workspace_assistant(ctx.tenant_id, workspace_id)
    return _assistant_response(persona)


class AssistRequest(BaseModel):
    task: str  # draft_persona | draft_knowledge | ask
    subject: str = ""
    instruction: str


class AssistResponse(BaseModel):
    text: str
    context_entry_keys: list[str]
    model: str


@router.post("/{workspace_id}/assist")
async def assist_endpoint(
    workspace_id: uuid.UUID,
    body: AssistRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> AssistResponse:
    persona = await ensure_workspace_assistant(ctx.tenant_id, workspace_id)
    profile = await get_agent(ctx.tenant_id, persona.agent_id)
    if profile is None:
        raise HTTPException(status_code=409, detail="the assistant's model profile is missing")
    if not profile.model:
        # A fresh install ships no model. Say that plainly here rather than letting the
        # call fail deep in the provider with a connection error nobody can act on.
        raise HTTPException(
            status_code=409,
            detail=(
                "no model is configured for the assistant yet -- set one on the "
                "'Assistant model' profile, or start the deployment with "
                "PYRRHULA_ASSISTANT_MODEL (and PYRRHULA_ASSISTANT_API_BASE for a local "
                "provider)"
            ),
        )

    async with tenant_scope(ctx.tenant_id) as session:
        viewer = await session.get(Principal, ctx.principal_id)
        if viewer is None:
            raise HTTPException(status_code=403, detail="no principal in this tenant")
        session.expunge(viewer)

    try:
        # A bounded wait: without it a wedged provider left the browser request pending
        # forever ("I asked and it never responded").
        result = await asyncio.wait_for(
            assist(
                ctx.tenant_id,
                workspace_id,
                viewer,
                task=body.task,
                subject=body.subject,
                instruction=body.instruction,
                profile=profile,
                provider=get_model_provider(profile.provider),
                embedder=get_embedding_provider(),
                api_key=await resolve_connection_api_key(
                    ctx.tenant_id, profile.credential_ref, encryptor=get_encryptor()
                ),
            ),
            timeout=_ASSIST_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=f"the assistant's model did not answer within {_ASSIST_TIMEOUT_S}s",
        ) from exc
    except UnknownAssistTaskError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EgressDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return AssistResponse(
        text=result.text, context_entry_keys=result.context_entry_keys, model=result.model_string
    )


class ChatMessage(BaseModel):
    role: str  # user | assistant | system
    content: str


class AssistantChatRequest(BaseModel):
    messages: list[ChatMessage]


@router.post("/{workspace_id}/assistant-chat")
async def assistant_chat_endpoint(
    workspace_id: uuid.UUID,
    body: AssistantChatRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> StreamingResponse:
    """The floating widget's streaming chat: NDJSON events (text deltas, tool markers,
    edit proposals, done/error). History arrives from the browser each time -- nothing
    is persisted server-side. Proposals are applied by the browser through the ordinary
    API endpoints, so the assistant's effective access is exactly the caller's."""
    from core.agents.assistant_chat import chat

    async with tenant_scope(ctx.tenant_id) as session:
        viewer = await session.get(Principal, ctx.principal_id)
        if viewer is None:
            raise HTTPException(status_code=403, detail="no principal in this tenant")
        session.expunge(viewer)

    async def _ndjson() -> AsyncIterator[str]:
        async for event in chat(
            ctx.tenant_id,
            workspace_id,
            viewer,
            [m.model_dump() for m in body.messages],
            embedder=get_embedding_provider(),
            provider_factory=get_model_provider,
            encryptor=get_encryptor(),
        ):
            yield json.dumps(event) + "\n"

    return StreamingResponse(_ndjson(), media_type="application/x-ndjson")


class RepoAnalysisRequest(BaseModel):
    repo_ids: list[uuid.UUID] = []


class RepoAnalysisResponse(BaseModel):
    job_id: uuid.UUID
    repo_ids: list[uuid.UUID]


@router.post("/{workspace_id}/repo-analysis", status_code=202)
async def repo_analysis_endpoint(
    workspace_id: uuid.UUID,
    body: RepoAnalysisRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> RepoAnalysisResponse:
    """Enqueue the repo knowledge-graph analysis for the given repos (default: every
    non-archived repo in the tenant's registry). The worker reads the hosted store trees,
    ingests each repo as knowledge, and writes the cross-repo overview + graph."""
    repos = {r.id: r for r in await list_repos(ctx.tenant_id)}
    if body.repo_ids:
        unknown = [rid for rid in body.repo_ids if rid not in repos]
        if unknown:
            raise HTTPException(status_code=404, detail=f"unknown repo ids {unknown}")
        selected = body.repo_ids
    else:
        selected = list(repos)
    if not selected:
        raise HTTPException(status_code=409, detail="no repos registered to analyze")

    job_id = await get_job_queue().enqueue(
        ctx.tenant_id,
        "analyze_workspace_repos",
        {
            "tenant_id": str(ctx.tenant_id),
            "workspace_id": str(workspace_id),
            "repo_ids": [str(rid) for rid in selected],
        },
    )
    return RepoAnalysisResponse(job_id=job_id, repo_ids=selected)


class RepoGraphResponse(BaseModel):
    available: bool
    overview_md: str = ""
    graph: dict[str, object] | None = None
    repo_summaries: dict[str, str] = {}


@router.get("/{workspace_id}/repo-graph")
async def repo_graph_endpoint(
    workspace_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> RepoGraphResponse:
    """The latest published repo-overview entries, shaped for the graph page. Reads the
    same knowledge rows every agent's context assembles from -- the graph is a rendering
    of workspace knowledge, not a parallel store."""
    from core.knowledge.repo_overview import list_published_overview_entries

    entries = await list_published_overview_entries(ctx.tenant_id, workspace_id)
    if not entries:
        return RepoGraphResponse(available=False)

    overview = ""
    graph: dict[str, object] | None = None
    summaries: dict[str, str] = {}
    for entry in entries:
        if entry.entry_key == "overview":
            overview = entry.body_md
        elif entry.entry_key == "graph":
            try:
                parsed = json.loads(entry.body_md)
                graph = parsed if isinstance(parsed, dict) else None
            except ValueError:
                graph = None
        elif entry.entry_key.startswith("repo-"):
            summaries[entry.entry_key.removeprefix("repo-")] = entry.body_md
    return RepoGraphResponse(
        available=True, overview_md=overview, graph=graph, repo_summaries=summaries
    )
