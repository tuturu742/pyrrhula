"""Overseer query endpoints (E2.10): the HTTP surface over `OverseerService` -- the
Director's View UI (E2.11) is this router's own consumer; the `overseer.query` MCP tool
(E2.12) calls the same `OverseerService` methods directly, not through HTTP, but both
paths audit identically since there is exactly one service underneath.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.encryptor_factory import get_encryptor
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from api.permission_service_factory import get_permission_service
from core.overseer.service import (
    OverseerPermissionDeniedError,
    OverseerService,
    SecretNotFoundError,
)
from core.ports.encryptor import Encryptor
from core.ports.permission import PermissionService, UnknownActionError
from core.tenancy.context import RequestContext

_INSPECT_ACTION = "secret:inspect"

router = APIRouter(
    prefix="/overseer",
    tags=["overseer"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


def _service(encryptor: Encryptor, permission_service: PermissionService) -> OverseerService:
    return OverseerService(encryptor=encryptor, permission_service=permission_service)


class InspectResponse(BaseModel):
    id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    content: str
    gist: str
    hint_text: str | None
    behavioral_directive: str | None
    disclosure_state: str


class HolderResponse(BaseModel):
    holder_principal_id: uuid.UUID
    holder_kind: str


class DisclosureEventResponse(BaseModel):
    session_id: uuid.UUID
    event_seq: int
    mode: str
    disclosed_to: dict[str, object]


class AgentBeliefResponse(BaseModel):
    secret_id: uuid.UUID
    holder_kind: str
    disclosure_state: str


class AccessResponse(BaseModel):
    can_inspect: bool


@router.get("/access")
async def check_access(
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    permission_service: PermissionService = Depends(get_permission_service),
) -> AccessResponse:
    """Lets the frontend decide whether to show the Director's View nav entry/route at
    all -- a plain permission check, not a secret-plaintext read, so it isn't routed
    through `OverseerService` and doesn't write an audit row."""
    try:
        granted = await permission_service.check(
            ctx.tenant_id, ctx.principal_id, _INSPECT_ACTION, "workspace", workspace_id
        )
    except UnknownActionError:
        granted = False
    return AccessResponse(can_inspect=granted)


@router.get("/secrets/{secret_id}")
async def inspect_secret(
    secret_id: uuid.UUID,
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    encryptor: Encryptor = Depends(get_encryptor),
    permission_service: PermissionService = Depends(get_permission_service),
) -> InspectResponse:
    service = _service(encryptor, permission_service)
    try:
        view = await service.inspect(
            ctx.tenant_id,
            ctx.principal_id,
            workspace_id,
            secret_id,
            query={"endpoint": "GET /overseer/secrets/{secret_id}"},
        )
    except OverseerPermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SecretNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return InspectResponse(
        id=view.id,
        subject_kind=view.subject_kind,
        subject_id=view.subject_id,
        content=view.content,
        gist=view.gist,
        hint_text=view.hint_text,
        behavioral_directive=view.behavioral_directive,
        disclosure_state=view.disclosure_state,
    )


@router.get("/secrets/{secret_id}/holders")
async def list_holders(
    secret_id: uuid.UUID,
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    encryptor: Encryptor = Depends(get_encryptor),
    permission_service: PermissionService = Depends(get_permission_service),
) -> list[HolderResponse]:
    service = _service(encryptor, permission_service)
    try:
        holders = await service.list_holders(
            ctx.tenant_id, ctx.principal_id, workspace_id, secret_id
        )
    except OverseerPermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return [
        HolderResponse(holder_principal_id=h.holder_principal_id, holder_kind=h.holder_kind)
        for h in holders
    ]


@router.get("/secrets/{secret_id}/timeline")
async def disclosure_timeline(
    secret_id: uuid.UUID,
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    encryptor: Encryptor = Depends(get_encryptor),
    permission_service: PermissionService = Depends(get_permission_service),
) -> list[DisclosureEventResponse]:
    service = _service(encryptor, permission_service)
    try:
        events = await service.disclosure_timeline(
            ctx.tenant_id, ctx.principal_id, workspace_id, secret_id
        )
    except OverseerPermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return [
        DisclosureEventResponse(
            session_id=e.session_id, event_seq=e.event_seq, mode=e.mode, disclosed_to=e.disclosed_to
        )
        for e in events
    ]


@router.get("/agents/{agent_principal_id}/beliefs")
async def agent_beliefs(
    agent_principal_id: uuid.UUID,
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    encryptor: Encryptor = Depends(get_encryptor),
    permission_service: PermissionService = Depends(get_permission_service),
) -> list[AgentBeliefResponse]:
    service = _service(encryptor, permission_service)
    try:
        beliefs = await service.agent_beliefs(
            ctx.tenant_id, ctx.principal_id, workspace_id, agent_principal_id
        )
    except OverseerPermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return [
        AgentBeliefResponse(
            secret_id=b.secret_id, holder_kind=b.holder_kind, disclosure_state=b.disclosure_state
        )
        for b in beliefs
    ]
