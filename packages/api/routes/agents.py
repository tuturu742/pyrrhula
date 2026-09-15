"""D1.5: agent + model-profile management -- persona, model profile, role type, provider
credentials. The UI never redisplays a key: ``AgentResponse`` only ever carries
``credential_ref`` (an opaque id), never ciphertext or plaintext, and there is no route
anywhere in this module that reads a stored credential back out.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import cast

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.authz import require_tenant_permission
from api.encryptor_factory import get_encryptor
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from api.model_provider_factory import get_model_provider
from core.agents.assistant import ASSISTANT_KEY
from core.agents.authoring import (
    AgentNotFoundError,
    PersonaNotFoundError,
    archive_agent,
    archive_persona,
    create_agent,
    create_persona,
    get_agent,
    get_persona,
    list_agents,
    list_personas,
    resolve_connection_api_key,
    update_agent,
    update_persona,
)
from core.agents.editing import apply_persona_edit_proposal, propose_persona_edit
from core.agents.models import Agent, Persona
from core.behavior.capabilities import list_capabilities_for_model
from core.behavior.repo import list_axis_definitions
from core.ports.model_provider import EgressDeniedError, GenerationRequest
from core.tenancy.context import RequestContext

_TEST_CONNECTION_TIMEOUT_S = 20

router = APIRouter(
    tags=["agents"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


# ── model profiles ──────────────────────────────────────────────────────────────────


class AgentResponse(BaseModel):
    id: uuid.UUID
    name: str
    provider: str
    model: str
    params: dict[str, object]
    credential_ref: str | None
    api_base: str | None
    fallback_agent_id: uuid.UUID | None


def _model_profile_response(row: Agent) -> AgentResponse:
    return AgentResponse(
        id=row.id,
        name=row.name,
        provider=row.provider,
        model=row.model,
        params=row.params,
        credential_ref=row.credential_ref,
        api_base=row.api_base,
        fallback_agent_id=row.fallback_agent_id,
    )


class CreateAgentRequest(BaseModel):
    name: str
    provider: str
    model: str
    params: dict[str, object] = {}
    api_key: str | None = None
    # Connection setup lives on the profile (e.g. a specific Ollama host:port), not a
    # process-wide env var -- lets a tenant point different profiles at different local
    # deployments.
    api_base: str | None = None
    fallback_agent_id: uuid.UUID | None = None


class UpdateAgentRequest(BaseModel):
    name: str | None = None
    provider: str | None = None
    model: str | None = None
    params: dict[str, object] | None = None
    api_key: str | None = None
    api_base: str | None = None
    fallback_agent_id: uuid.UUID | None = None


class CapabilitiesResponse(BaseModel):
    supports_tools: bool
    supports_json_mode: bool
    supports_prompt_caching: bool


@router.get("/model-profiles/capabilities")
async def get_capabilities(
    provider: str, model: str, _ctx: RequestContext = Depends(get_request_context)
) -> CapabilitiesResponse:
    """Registered before `/model-profiles/{agent_id}` for the same literal-vs-
    path-param routing reason D1.2's `/templates` endpoint documents."""
    caps = get_model_provider(provider).capabilities(model)
    return CapabilitiesResponse(
        supports_tools=caps.supports_tools,
        supports_json_mode=caps.supports_json_mode,
        supports_prompt_caching=caps.supports_prompt_caching,
    )


class AxisCapabilityResponse(BaseModel):
    axis_key: str
    stakes: str
    capable: bool
    reason: str | None


@router.get("/model-profiles/axis-capabilities")
async def get_axis_capabilities(
    provider: str,
    model: str,
    pack_id: str,
    ctx: RequestContext = Depends(get_request_context),
) -> list[AxisCapabilityResponse]:
    """E2.9: the UI's "unavailable on this model" state for a behavior-profile axis
    control -- one row per axis in `pack_id`, with the machine-readable reason code a
    disabled control needs (never just a disabled boolean with no explanation)."""
    axes = await list_axis_definitions(ctx.tenant_id, pack_id)
    stored = {c.axis_key: c for c in await list_capabilities_for_model(provider, model)}
    return [
        AxisCapabilityResponse(
            axis_key=axis.key,
            stakes=axis.stakes,
            capable=stored[axis.key].capable if axis.key in stored else True,
            reason=stored[axis.key].reason if axis.key in stored else None,
        )
        for axis in axes
    ]


class GateModelBody(BaseModel):
    # None clears the choice: the gate then runs on each acting persona's own connection.
    connection_id: uuid.UUID | None = None


@router.get("/model-profiles/gate")
async def get_gate_model_endpoint(
    ctx: RequestContext = Depends(get_request_context),
) -> GateModelBody:
    """Which connection runs this tenant's disclosure gate."""
    from core.secrets.gate_config import get_gate_connection_id

    return GateModelBody(connection_id=await get_gate_connection_id(ctx.tenant_id))


@router.put("/model-profiles/gate")
async def set_gate_model_endpoint(
    body: GateModelBody,
    ctx: RequestContext = Depends(get_request_context),
) -> GateModelBody:
    """Point the gate at one of this tenant's connections.

    A tenant decision, not a deployment one: a deployment hosts many tenants and they do
    not share a model choice. Gated on `manage_tenant` because it selects what an
    security-relevant classifier runs on."""
    from api.authz import require_tenant_permission
    from core.secrets.gate_config import set_gate_connection_id

    await require_tenant_permission(ctx, "manage_tenant")
    try:
        chosen = await set_gate_connection_id(ctx.tenant_id, body.connection_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return GateModelBody(connection_id=chosen)


@router.post("/model-profiles", status_code=201)
async def create_agent_endpoint(
    body: CreateAgentRequest, ctx: RequestContext = Depends(get_request_context)
) -> AgentResponse:
    profile = await create_agent(
        ctx.tenant_id,
        body.name,
        body.provider,
        body.model,
        params=body.params,
        api_key=body.api_key,
        api_base=body.api_base,
        fallback_agent_id=body.fallback_agent_id,
        encryptor=get_encryptor(),
    )
    return _model_profile_response(profile)


@router.get("/model-profiles")
async def list_agents_endpoint(
    ctx: RequestContext = Depends(get_request_context),
) -> list[AgentResponse]:
    profiles = await list_agents(ctx.tenant_id)
    return [_model_profile_response(p) for p in profiles]


class AvailableModelsResponse(BaseModel):
    models: list[str]
    detail: str = ""


async def _list_provider_models(
    provider: str, api_base: str | None, api_key: str | None
) -> AvailableModelsResponse:
    """Best-effort model listing per provider. Failures come back as ``detail``, never a
    500 -- the model field stays freely typeable either way."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if provider == "ollama":
                base = (api_base or "http://localhost:11434").rstrip("/")
                resp = await client.get(f"{base}/api/tags")
                resp.raise_for_status()
                return AvailableModelsResponse(
                    models=sorted(m["name"] for m in resp.json().get("models", []))
                )
            if provider == "openai":
                if not api_key:
                    return AvailableModelsResponse(models=[], detail="an API key is required")
                base = (api_base or "https://api.openai.com/v1").rstrip("/")
                resp = await client.get(
                    f"{base}/models", headers={"Authorization": f"Bearer {api_key}"}
                )
                resp.raise_for_status()
                return AvailableModelsResponse(
                    models=sorted(m["id"] for m in resp.json().get("data", []))
                )
            if provider == "anthropic":
                if not api_key:
                    return AvailableModelsResponse(models=[], detail="an API key is required")
                resp = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                )
                resp.raise_for_status()
                return AvailableModelsResponse(
                    models=sorted(m["id"] for m in resp.json().get("data", []))
                )
            if provider == "gemini":
                if not api_key:
                    return AvailableModelsResponse(models=[], detail="an API key is required")
                resp = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": api_key},
                )
                resp.raise_for_status()
                return AvailableModelsResponse(
                    models=sorted(
                        m["name"].removeprefix("models/") for m in resp.json().get("models", [])
                    )
                )
    except Exception as exc:  # noqa: BLE001 -- report the reason, don't 500
        return AvailableModelsResponse(
            models=[], detail=f"could not list models for {provider}: {exc}"[:300]
        )
    return AvailableModelsResponse(
        models=[], detail=f"no model listing for provider {provider!r}; enter the model name"
    )


# Registered before "/model-profiles/{agent_id}" -- a literal segment must be matched before
# the path-param route, or FastAPI tries (and fails, 422) to parse "available-models" as a uuid.
@router.get("/model-profiles/available-models")
async def available_models_endpoint(
    provider: str,
    api_base: str | None = None,
    ctx: RequestContext = Depends(get_request_context),
) -> AvailableModelsResponse:
    """#3 (legacy GET, keyless): list models the provider offers. Cloud providers need a
    key -- use the POST variant, which can fall back to a saved profile's stored key."""
    return await _list_provider_models(provider, api_base, None)


class AvailableModelsRequest(BaseModel):
    provider: str
    api_base: str | None = None
    # A key typed in the form but not yet saved; blank while editing = use the stored key.
    api_key: str | None = None
    model_profile_id: uuid.UUID | None = None


@router.post("/model-profiles/available-models")
async def available_models_post_endpoint(
    body: AvailableModelsRequest, ctx: RequestContext = Depends(get_request_context)
) -> AvailableModelsResponse:
    """Model listing with credentials: form key, else the named profile's stored key
    (same fallback shape as test-connection). The key is used transiently, never logged
    or echoed."""
    api_key = body.api_key
    if not api_key and body.model_profile_id is not None:
        profile = await get_agent(ctx.tenant_id, body.model_profile_id)
        if profile is not None:
            api_key = await resolve_connection_api_key(
                ctx.tenant_id, profile.credential_ref, encryptor=get_encryptor()
            )
    return await _list_provider_models(body.provider, body.api_base, api_key)


@router.get("/model-profiles/{agent_id}")
async def get_agent_endpoint(
    agent_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> AgentResponse:
    profile = await get_agent(ctx.tenant_id, agent_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"no model profile {agent_id}")
    return _model_profile_response(profile)


@router.patch("/model-profiles/{agent_id}")
async def update_agent_endpoint(
    agent_id: uuid.UUID,
    body: UpdateAgentRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> AgentResponse:
    # api_base: None is a legal value ("clear the override"), not "leave it alone" --
    # same model_fields_set distinction UpdatePersonaRequest.entity_id needs.
    api_base_arg: str | None | object = (
        body.api_base if "api_base" in body.model_fields_set else ...
    )
    try:
        profile = await update_agent(
            ctx.tenant_id,
            agent_id,
            name=body.name,
            provider=body.provider,
            model=body.model,
            params=body.params,
            api_key=body.api_key,
            api_base=api_base_arg,
            fallback_agent_id=body.fallback_agent_id,
            encryptor=get_encryptor(),
        )
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _model_profile_response(profile)


@router.delete("/model-profiles/{agent_id}", status_code=204)
async def archive_agent_endpoint(
    agent_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> None:
    """Soft-delete (archive) a model profile so it drops out of the profile picker. Agents
    still pinned to it keep their reference; the profile is hidden, not removed."""
    await require_tenant_permission(ctx, "agent:archive")
    try:
        await archive_agent(ctx.tenant_id, agent_id)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class TestConnectionResponse(BaseModel):
    ok: bool
    detail: str


@router.post("/model-profiles/{agent_id}/test")
async def test_model_profile_endpoint(
    agent_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> TestConnectionResponse:
    """A real, minimal ``generate()`` call through the same provider/model string a live
    turn would use (``runtime._call_provider_with_retry``'s own
    ``f"{provider}/{model}"``) -- not a static capability lookup. The stored key (if any) is
    decrypted and passed exactly as a live turn does, so a cloud connection with a key on
    file is tested for real; Ollama needs no key."""
    profile = await get_agent(ctx.tenant_id, agent_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"no model profile {agent_id}")

    api_key = await resolve_connection_api_key(
        ctx.tenant_id, profile.credential_ref, encryptor=get_encryptor()
    )
    return await _run_connection_test(profile.provider, profile.model, profile.api_base, api_key)


async def _run_connection_test(
    provider_kind: str, model: str, api_base: str | None, api_key: str | None
) -> TestConnectionResponse:
    """The shared 'ask the provider to say OK' probe, used by both the by-id test (a saved
    connection) and the test-before-save form (#2)."""
    provider = get_model_provider(provider_kind)
    model_string = f"{provider_kind}/{model}"
    # A connection-test probe is operator diagnostics, not tenant content -- egress
    # policy (D14) does not apply; nothing tenant-authored is in the payload.
    req = GenerationRequest(
        model=model_string,
        messages=[{"role": "user", "content": "Reply with the single word OK."}],
        purpose="generation",
        max_tokens=8,
        api_base=api_base,
        api_key=api_key,
    )

    async def _generate() -> None:
        async for _chunk in provider.generate(req):
            pass

    try:
        await asyncio.wait_for(_generate(), timeout=_TEST_CONNECTION_TIMEOUT_S)
    except TimeoutError:
        return TestConnectionResponse(
            ok=False, detail=f"{model_string} timed out after {_TEST_CONNECTION_TIMEOUT_S}s"
        )
    except EgressDeniedError as exc:
        return TestConnectionResponse(ok=False, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 -- surfacing the real provider error is the point
        return TestConnectionResponse(ok=False, detail=str(exc)[:500])
    return TestConnectionResponse(ok=True, detail=f"{model_string} responded")


class TestConnectionRequest(BaseModel):
    provider: str
    model: str
    api_base: str | None = None
    # A key typed in the form but not yet saved. Optional -- a local provider needs none, and
    # editing an existing cloud connection can rely on its stored key (leave blank).
    api_key: str | None = None
    # When editing, the profile whose stored key to fall back on if api_key is blank.
    model_profile_id: uuid.UUID | None = None


@router.post("/model-profiles/test-connection")
async def test_connection_endpoint(
    body: TestConnectionRequest, ctx: RequestContext = Depends(get_request_context)
) -> TestConnectionResponse:
    """#2: test a connection from the form's own values, before it's saved. Falls back to the
    named profile's stored key when the form key is blank (editing an existing connection)."""
    api_key = body.api_key
    if not api_key and body.model_profile_id is not None:
        profile = await get_agent(ctx.tenant_id, body.model_profile_id)
        if profile is not None:
            api_key = await resolve_connection_api_key(
                ctx.tenant_id, profile.credential_ref, encryptor=get_encryptor()
            )
    return await _run_connection_test(body.provider, body.model, body.api_base, api_key)


# ── agents ───────────────────────────────────────────────────────────────────────────


class PersonaResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    key: str
    name: str
    persona_type: str
    persona_md: str
    entity_id: uuid.UUID | None
    agent_id: uuid.UUID
    web_search: bool = False
    # Per-persona generation overrides, merged over the connection's params -- distinct
    # voices on a shared connection without cloning it.
    params: dict[str, object] = {}


def _agent_response(row: Persona) -> PersonaResponse:
    return PersonaResponse(
        id=row.id,
        workspace_id=row.workspace_id,
        key=row.key,
        name=row.name,
        persona_type=row.persona_type,
        persona_md=row.persona_md,
        entity_id=row.entity_id,
        agent_id=row.agent_id,
        web_search=row.web_search,
        params=dict(row.params or {}),
    )


class StarterTeamRequest(BaseModel):
    workspace_id: uuid.UUID
    agent_id: uuid.UUID  # the model connection every starter persona binds to


@router.post("/agents/starter-team", status_code=201)
async def starter_team_endpoint(
    body: StarterTeamRequest, ctx: RequestContext = Depends(get_request_context)
) -> list[PersonaResponse]:
    """Onboarding shortcut: a working roster in one click -- Lead (supervisor) + two
    participants, each with the workspace role agent personas need to act on entities
    and take turns (supervisor->facilitator, participant->participant). Idempotent by
    persona key; safe to call on a workspace that already has some of them."""
    from sqlalchemy import select as _select

    from core.tenancy.models import WorkspaceMembership
    from core.tenancy.scope import tenant_scope

    roster = [
        (
            "lead",
            "Lead",
            "supervisor",
            "You are the team lead. Frame the work, keep discussion on the agenda, make "
            "the final call, review delegated work critically.",
        ),
        (
            "dev-a",
            "Dev A",
            "participant",
            "You are a pragmatic builder. Propose concrete approaches and flag risks.",
        ),
        (
            "dev-b",
            "Dev B",
            "participant",
            "You are a detail-oriented reviewer. Probe edge cases and gaps in proposals.",
        ),
    ]
    role_for = {"supervisor": "facilitator", "participant": "participant"}
    created: list[PersonaResponse] = []
    existing = {p.key for p in await list_personas(ctx.tenant_id, body.workspace_id)}
    for key, name, persona_type, persona_md in roster:
        if key in existing:
            continue
        persona = await create_persona(
            ctx.tenant_id,
            body.workspace_id,
            key,
            name,
            body.agent_id,
            persona_type=persona_type,
            persona_md=persona_md,
        )
        async with tenant_scope(ctx.tenant_id) as session:
            has = await session.scalar(
                _select(WorkspaceMembership.id).where(
                    WorkspaceMembership.workspace_id == body.workspace_id,
                    WorkspaceMembership.principal_id == persona.principal_id,
                )
            )
            if has is None:
                session.add(
                    WorkspaceMembership(
                        tenant_id=ctx.tenant_id,
                        workspace_id=body.workspace_id,
                        principal_id=persona.principal_id,
                        role=role_for[persona_type],
                    )
                )
        created.append(_agent_response(persona))
    return created


class CreatePersonaRequest(BaseModel):
    workspace_id: uuid.UUID
    key: str
    name: str
    agent_id: uuid.UUID
    persona_type: str = "participant"
    persona_md: str = ""
    entity_id: uuid.UUID | None = None
    # Per-persona internet-search switch. Enabling it also registers the workspace's
    # `web_search` MCP server (the allowlist row is the egress control).
    web_search: bool = False
    params: dict[str, object] = {}


class UpdatePersonaRequest(BaseModel):
    name: str | None = None
    persona_type: str | None = None
    persona_md: str | None = None
    entity_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    web_search: bool | None = None
    params: dict[str, object] | None = None


async def _ensure_web_search_server(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    """Enabling any persona's search switch also puts the `web_search` MCP server on the
    workspace allowlist (idempotent) -- the allowlist row is the inspectable, revocable
    egress control (CLAUDE.md rule 11; core.mcp.registry.WEB_SEARCH_PRESET's shape). The
    URL comes from deployment config (a SearXNG-compatible JSON endpoint); without one the
    preset's unreachable placeholder registers structure with no egress."""
    from core.config import get_settings
    from core.mcp.registry import WEB_SEARCH_PRESET, register_server

    url = get_settings().web_search_url or str(WEB_SEARCH_PRESET["url"])
    await register_server(
        tenant_id,
        workspace_id,
        str(WEB_SEARCH_PRESET["key"]),
        url,
        enabled_tools=[str(t) for t in cast(list[str], WEB_SEARCH_PRESET["enabled_tools"])],
        effectful_tools=[str(t) for t in cast(list[str], WEB_SEARCH_PRESET["effectful_tools"])],
        require_confirmation=bool(WEB_SEARCH_PRESET["require_confirmation"]),
    )


@router.post("/agents", status_code=201)
async def create_persona_endpoint(
    body: CreatePersonaRequest, ctx: RequestContext = Depends(get_request_context)
) -> PersonaResponse:
    agent = await create_persona(
        ctx.tenant_id,
        body.workspace_id,
        body.key,
        body.name,
        body.agent_id,
        persona_type=body.persona_type,
        persona_md=body.persona_md,
        entity_id=body.entity_id,
        web_search=body.web_search,
        params=body.params,
    )
    if body.web_search:
        await _ensure_web_search_server(ctx.tenant_id, body.workspace_id)
    return _agent_response(agent)


@router.get("/agents")
async def list_personas_endpoint(
    workspace_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> list[PersonaResponse]:
    agents = await list_personas(ctx.tenant_id, workspace_id)
    return [_agent_response(a) for a in agents]


@router.get("/agents/{persona_id}")
async def get_persona_endpoint(
    persona_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> PersonaResponse:
    agent = await get_persona(ctx.tenant_id, persona_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"no agent {persona_id}")
    return _agent_response(agent)


@router.patch("/agents/{persona_id}")
async def update_persona_endpoint(
    persona_id: uuid.UUID,
    body: UpdatePersonaRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> PersonaResponse:
    # entity_id needs "was this field even in the request body" (None is a legal value --
    # unlink the entity -- not "leave it alone"), not just "is it None" -- Pydantic's
    # model_fields_set is exactly that distinction.
    entity_id_arg: uuid.UUID | None | object = (
        body.entity_id if "entity_id" in body.model_fields_set else ...
    )
    try:
        agent = await update_persona(
            ctx.tenant_id,
            persona_id,
            name=body.name,
            persona_type=body.persona_type,
            persona_md=body.persona_md,
            entity_id=entity_id_arg,
            agent_id=body.agent_id,
            web_search=body.web_search,
            params=body.params,
        )
    except PersonaNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if body.web_search:
        await _ensure_web_search_server(ctx.tenant_id, agent.workspace_id)
    return _agent_response(agent)


# ── behavior profile (E2.9's user surface: the sliders) ──────────────────────────────
class AxisOut(BaseModel):
    key: str
    label_key: str
    range_min: int
    range_max: int
    default: int
    stakes: str
    semantics_md: str
    has_prompt_directive: bool
    has_gate: bool


class BehaviorProfileOut(BaseModel):
    pack_id: str | None
    packs: list[str]
    version: int
    axis_values: dict[str, int]
    axes: list[AxisOut]


class PutBehaviorProfileRequest(BaseModel):
    pack_id: str
    axis_values: dict[str, int]


async def _behavior_axes(tenant_id: uuid.UUID, pack_id: str) -> list[AxisOut]:
    from core.behavior.repo import list_axis_definitions

    return [
        AxisOut(
            key=a.key,
            label_key=a.label_key,
            range_min=a.range_min,
            range_max=a.range_max,
            default=(
                a.default_value if a.default_value is not None else (a.range_min + a.range_max) // 2
            ),
            stakes=a.stakes,
            semantics_md=a.semantics_md,
            has_prompt_directive=any(b.get("kind") == "prompt_directive" for b in a.bindings),
            has_gate=any(b.get("kind") == "gate" for b in a.bindings),
        )
        for a in await list_axis_definitions(tenant_id, pack_id)
    ]


@router.get("/agents/{persona_id}/behavior")
async def get_behavior_profile_endpoint(
    persona_id: uuid.UUID,
    pack_id: str | None = None,
    ctx: RequestContext = Depends(get_request_context),
) -> BehaviorProfileOut:
    """The persona's current disposition (latest profile version) plus the axis
    definitions a slider UI needs (label, range, stakes, semantics). `pack_id` defaults
    to the current profile's pack, else the tenant's only loaded pack."""
    from sqlalchemy import select as sa_select

    from core.behavior.models import AxisDefinitionRow
    from core.behavior.repo import get_current_behavior_profile
    from core.tenancy.scope import tenant_scope

    if await get_persona(ctx.tenant_id, persona_id) is None:
        raise HTTPException(status_code=404, detail=f"no persona {persona_id}")

    async with tenant_scope(ctx.tenant_id) as session:
        packs = sorted(
            (await session.execute(sa_select(AxisDefinitionRow.pack_id).distinct())).scalars()
        )
    profile = await get_current_behavior_profile(ctx.tenant_id, persona_id)
    resolved = (
        pack_id or (profile.pack_id if profile else None) or (packs[0] if len(packs) == 1 else None)
    )
    return BehaviorProfileOut(
        pack_id=resolved,
        packs=packs,
        version=profile.version if profile else 0,
        axis_values=dict(profile.axis_values) if profile else {},
        axes=await _behavior_axes(ctx.tenant_id, resolved) if resolved else [],
    )


@router.put("/agents/{persona_id}/behavior")
async def put_behavior_profile_endpoint(
    persona_id: uuid.UUID,
    body: PutBehaviorProfileRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> BehaviorProfileOut:
    """Set the sliders: appends a NEW profile version (history is immutable -- past
    manifests pin the version that was in effect). Values are validated against the
    pack's axis definitions; unknown keys and out-of-range values are 422s."""
    from core.behavior.repo import (
        AxisValueOutOfRangeError,
        UnknownAxisError,
        create_behavior_profile,
    )

    if await get_persona(ctx.tenant_id, persona_id) is None:
        raise HTTPException(status_code=404, detail=f"no persona {persona_id}")
    try:
        profile = await create_behavior_profile(
            ctx.tenant_id,
            persona_id,
            body.pack_id,
            {k: int(v) for k, v in body.axis_values.items()},
            created_by=ctx.principal_id,
        )
    except (UnknownAxisError, AxisValueOutOfRangeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return BehaviorProfileOut(
        pack_id=profile.pack_id,
        packs=[profile.pack_id],
        version=profile.version,
        axis_values=dict(profile.axis_values),
        axes=await _behavior_axes(ctx.tenant_id, profile.pack_id),
    )


@router.delete("/agents/{persona_id}", status_code=204)
async def archive_persona_endpoint(
    persona_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> None:
    """Soft-delete (archive) an agent -- it leaves every roster/picker and never takes
    another turn, but its history is preserved. True removal is the superuser purge CLI."""
    await require_tenant_permission(ctx, "agent:archive")
    persona = await get_persona(ctx.tenant_id, persona_id)
    if persona is not None and persona.key == ASSISTANT_KEY:
        raise HTTPException(
            status_code=409, detail="the workspace assistant is required and cannot be archived"
        )
    try:
        await archive_persona(ctx.tenant_id, persona_id)
    except PersonaNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class ProposePersonaEditRequest(BaseModel):
    agent_id: uuid.UUID
    instruction: str


class PersonaEditProposalResponse(BaseModel):
    current_persona_md: str
    proposed_persona_md: str
    text_diff: str
    valid: bool
    issues: list[str]


class ApplyPersonaEditRequest(BaseModel):
    proposed_persona_md: str


@router.post("/agents/{persona_id}/propose-persona-edit")
async def propose_persona_edit_endpoint(
    persona_id: uuid.UUID,
    body: ProposePersonaEditRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> PersonaEditProposalResponse:
    """F3.12: draft-and-approve for an agent's persona -- this endpoint only ever
    proposes. Approving is the separate ``POST .../apply-persona-edit`` call below."""
    agent = await get_agent(ctx.tenant_id, body.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="no such model profile")
    try:
        proposal = await propose_persona_edit(
            ctx.tenant_id,
            persona_id,
            body.instruction,
            agent=agent,
            provider=get_model_provider(agent.provider),
            api_key=await resolve_connection_api_key(
                ctx.tenant_id, agent.credential_ref, encryptor=get_encryptor()
            ),
        )
    except PersonaNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PersonaEditProposalResponse(
        current_persona_md=proposal.current_persona_md,
        proposed_persona_md=proposal.proposed_persona_md,
        text_diff=proposal.text_diff,
        valid=proposal.valid,
        issues=proposal.issues,
    )


@router.post("/agents/{persona_id}/apply-persona-edit")
async def apply_persona_edit_endpoint(
    persona_id: uuid.UUID,
    body: ApplyPersonaEditRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> PersonaResponse:
    """The only write path an *approved* proposal takes -- the human calling this
    endpoint is the approval; there is no separate "are you sure" step server-side, the
    same discipline E2.2's ``PATCH /secrets/{id}`` accept flow already established."""
    try:
        agent = await apply_persona_edit_proposal(
            ctx.tenant_id, persona_id, body.proposed_persona_md, approved_by=ctx.principal_id
        )
    except PersonaNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _agent_response(agent)
