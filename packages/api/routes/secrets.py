"""Secret authoring endpoints: create/edit a secret's four faces, manage its
holder set, and AI-assisted drafting of the directive/hint. Talks to
`core.secrets.authoring`/`core.secrets.drafting` — never `core.secrets.repo`, which INV-1
reserves for `core.assembler`/`core.overseer` (see `core.secrets.repo`'s docstring for why
authoring is exempt, mirroring `packages/api/routes/knowledge.py`'s identical relationship
to `core.knowledge.repo`).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.encryptor_factory import get_encryptor
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from api.model_provider_factory import get_model_provider
from api.moderation_provider_factory import get_moderation_provider
from api.permission_service_factory import get_permission_service
from core.agents.authoring import get_agent
from core.ports.encryptor import Encryptor
from core.ports.moderation import ModerationProvider
from core.ports.permission import PermissionService
from core.secrets.authoring import (
    UNSET,
    SecretAccessDeniedError,
    SecretContentRejectedError,
    SecretNotFoundError,
    SecretView,
    add_holder,
    create_secret,
    get_secret_view,
    list_holders,
    list_secret_views_for_workspace,
    remove_holder,
    update_secret_fields,
)
from core.secrets.drafting import DraftContainsPlaintextError, draft_directive_and_hint
from core.tenancy.context import RequestContext

router = APIRouter(
    prefix="/secrets",
    tags=["secrets"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)

_UNSET_MARKER = "__unset__"


class CreateSecretRequest(BaseModel):
    workspace_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    content: str
    gist: str
    scope_key: str
    hint_text: str | None = None
    behavioral_directive: str | None = None
    # "guarded" (default) keeps the plaintext inside the deployment: readable only by an
    # author, and exportable only into a password-sealed bundle. "publishable" says this
    # content was written to be handed out -- a character brief, a sample case.
    publication: str = "guarded"


class UpdateSecretRequest(BaseModel):
    content: str | None = None
    gist: str | None = None
    # `_UNSET_MARKER` (never a real hint/directive) distinguishes "omitted" from "set to
    # null" over the wire, the same way core.secrets.authoring's `UNSET` sentinel does
    # in-process -- JSON has no native "field absent vs field null" ambiguity-free wire
    # signal once Pydantic's `exclude_unset` is out of the picture, so this endpoint
    # spells "leave unchanged" as the literal string `_UNSET_MARKER` rather than omission.
    hint_text: str | None = _UNSET_MARKER
    behavioral_directive: str | None = _UNSET_MARKER
    publication: str | None = None


class SecretResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    # Who this secret is about, by NAME. The list used to show "agent · undisclosed"
    # for every row -- eleven identical labels on a murder mystery's secret list, with
    # no way to tell whose motive was whose without opening each one.
    subject_name: str | None = None
    gist: str
    disclosure_state: str
    scope_key: str
    publication: str
    version: int
    content: str | None
    hint_text: str | None
    behavioral_directive: str | None
    is_author: bool


class AddHolderRequest(BaseModel):
    holder_principal_id: uuid.UUID
    holder_kind: str


class HolderResponse(BaseModel):
    id: uuid.UUID
    holder_principal_id: uuid.UUID
    holder_kind: str
    holder_name: str | None = None


class DraftRequest(BaseModel):
    agent_id: uuid.UUID


class DraftResponse(BaseModel):
    behavioral_directive: str
    hint_text: str


def _secret_response(view: SecretView) -> SecretResponse:
    return SecretResponse(
        id=view.id,
        workspace_id=view.workspace_id,
        subject_kind=view.subject_kind,
        subject_id=view.subject_id,
        gist=view.gist,
        disclosure_state=view.disclosure_state,
        scope_key=view.scope_key,
        publication=view.publication,
        version=view.version,
        content=view.content,
        hint_text=view.hint_text,
        behavioral_directive=view.behavioral_directive,
        is_author=view.is_author,
    )


@router.get("")
async def list_secrets_endpoint(
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    encryptor: Encryptor = Depends(get_encryptor),
    permission_service: PermissionService = Depends(get_permission_service),
) -> list[SecretResponse]:
    views = await list_secret_views_for_workspace(
        ctx.tenant_id,
        workspace_id,
        ctx.principal_id,
        encryptor=encryptor,
        permission_service=permission_service,
    )
    return [_secret_response(v) for v in views]


@router.post("", status_code=201)
async def create_secret_endpoint(
    body: CreateSecretRequest,
    ctx: RequestContext = Depends(get_request_context),
    encryptor: Encryptor = Depends(get_encryptor),
    permission_service: PermissionService = Depends(get_permission_service),
    moderation_provider: ModerationProvider = Depends(get_moderation_provider),
) -> SecretResponse:
    try:
        row = await create_secret(
            ctx.tenant_id,
            body.workspace_id,
            ctx.principal_id,
            subject_kind=body.subject_kind,
            subject_id=body.subject_id,
            content=body.content,
            gist=body.gist,
            scope_key=body.scope_key,
            publication=body.publication,
            encryptor=encryptor,
            permission_service=permission_service,
            moderation_provider=moderation_provider,
            hint_text=body.hint_text,
            behavioral_directive=body.behavioral_directive,
        )
    except SecretAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SecretContentRejectedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    view = await get_secret_view(
        ctx.tenant_id,
        row.id,
        ctx.principal_id,
        encryptor=encryptor,
        permission_service=permission_service,
    )
    return _secret_response(view)


@router.get("/{secret_id}")
async def get_secret_endpoint(
    secret_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    encryptor: Encryptor = Depends(get_encryptor),
    permission_service: PermissionService = Depends(get_permission_service),
) -> SecretResponse:
    """Plaintext (`content`/`hint_text`/`behavioral_directive`) is populated only for a
    principal passing `secret:author` on the secret's workspace -- everyone else gets
    the same response shape with those three fields `null` and `gist` intact."""
    try:
        view = await get_secret_view(
            ctx.tenant_id,
            secret_id,
            ctx.principal_id,
            encryptor=encryptor,
            permission_service=permission_service,
        )
    except SecretNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _secret_response(view)


@router.patch("/{secret_id}")
async def update_secret_endpoint(
    secret_id: uuid.UUID,
    body: UpdateSecretRequest,
    ctx: RequestContext = Depends(get_request_context),
    encryptor: Encryptor = Depends(get_encryptor),
    permission_service: PermissionService = Depends(get_permission_service),
    moderation_provider: ModerationProvider = Depends(get_moderation_provider),
) -> SecretResponse:
    hint_text: str | None | object = body.hint_text if body.hint_text != _UNSET_MARKER else UNSET
    behavioral_directive: str | None | object = (
        body.behavioral_directive if body.behavioral_directive != _UNSET_MARKER else UNSET
    )
    try:
        row = await update_secret_fields(
            ctx.tenant_id,
            secret_id,
            ctx.principal_id,
            encryptor=encryptor,
            permission_service=permission_service,
            moderation_provider=moderation_provider,
            content=body.content,
            gist=body.gist,
            hint_text=hint_text,
            behavioral_directive=behavioral_directive,
            publication=body.publication,
        )
    except SecretNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SecretAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SecretContentRejectedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    view = await get_secret_view(
        ctx.tenant_id,
        row.id,
        ctx.principal_id,
        encryptor=encryptor,
        permission_service=permission_service,
    )
    return _secret_response(view)


@router.post("/{secret_id}/holders", status_code=201)
async def add_holder_endpoint(
    secret_id: uuid.UUID,
    body: AddHolderRequest,
    ctx: RequestContext = Depends(get_request_context),
    permission_service: PermissionService = Depends(get_permission_service),
) -> HolderResponse:
    try:
        row = await add_holder(
            ctx.tenant_id,
            secret_id,
            ctx.principal_id,
            body.holder_principal_id,
            body.holder_kind,
            permission_service=permission_service,
        )
    except SecretNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SecretAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return HolderResponse(
        id=row.id, holder_principal_id=row.holder_principal_id, holder_kind=row.holder_kind
    )


@router.get("/{secret_id}/holders")
async def list_holders_endpoint(
    secret_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> list[HolderResponse]:
    rows = await list_holders(ctx.tenant_id, secret_id)
    # Holders are principals; show them as the personas (or humans) they are, not ids.
    from sqlalchemy import select

    from core.agents.models import Persona
    from core.tenancy.models import Principal
    from core.tenancy.scope import tenant_scope

    holder_names: dict[uuid.UUID, str] = {}
    principal_ids = {r.holder_principal_id for r in rows}
    if principal_ids:
        async with tenant_scope(ctx.tenant_id) as session:
            for pid, name in await session.execute(
                select(Persona.principal_id, Persona.name).where(
                    Persona.principal_id.in_(principal_ids)
                )
            ):
                holder_names[pid] = name
            unresolved = principal_ids - set(holder_names)
            if unresolved:
                for pid, name in await session.execute(
                    select(Principal.id, Principal.display_name).where(Principal.id.in_(unresolved))
                ):
                    holder_names.setdefault(pid, name)
    return [
        HolderResponse(
            id=r.id,
            holder_principal_id=r.holder_principal_id,
            holder_kind=r.holder_kind,
            holder_name=holder_names.get(r.holder_principal_id),
        )
        for r in rows
    ]


@router.delete("/{secret_id}/holders/{holder_id}", status_code=204)
async def remove_holder_endpoint(
    secret_id: uuid.UUID,
    holder_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    permission_service: PermissionService = Depends(get_permission_service),
) -> None:
    try:
        await remove_holder(
            ctx.tenant_id,
            secret_id,
            ctx.principal_id,
            holder_id,
            permission_service=permission_service,
        )
    except SecretNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SecretAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/{secret_id}/draft")
async def draft_endpoint(
    secret_id: uuid.UUID,
    body: DraftRequest,
    ctx: RequestContext = Depends(get_request_context),
    encryptor: Encryptor = Depends(get_encryptor),
    permission_service: PermissionService = Depends(get_permission_service),
) -> DraftResponse:
    """Draft-and-approve, not autopilot: this endpoint only ever proposes. Saving
    the draft is a separate `PATCH /secrets/{id}` call the client makes only on explicit
    author acceptance -- nothing here writes to the secret itself."""
    try:
        view = await get_secret_view(
            ctx.tenant_id,
            secret_id,
            ctx.principal_id,
            encryptor=encryptor,
            permission_service=permission_service,
        )
    except SecretNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not view.is_author or view.content is None:
        raise HTTPException(
            status_code=403, detail="only a principal with secret:author may request a draft"
        )

    agent = await get_agent(ctx.tenant_id, body.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="no such model profile")

    provider = get_model_provider(agent.provider)
    try:
        draft = await draft_directive_and_hint(
            ctx.tenant_id,
            view.content,
            agent=agent,
            provider=provider,
            workspace_id=view.workspace_id,
        )
    except DraftContainsPlaintextError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return DraftResponse(behavioral_directive=draft.behavioral_directive, hint_text=draft.hint_text)
