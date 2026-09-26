"""ProcessDefinition authoring endpoints: create (= publish a new immutable
version), list, get, and a dry-run ``/validate`` that never persists anything -- the
live-feedback path the editor calls on every keystroke/save-attempt.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.authz import require_tenant_permission
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from core.process.authoring import (
    DefinitionValidationError,
    archive_definition,
    create_definition,
    get_definition,
    list_definitions,
    validate_document,
)
from core.process.dsl.fixtures import (
    AGENT_ROUND_TABLE_FLOW,
    MINIMAL_MVP_FLOW,
    STANDARD_SESSION_FLOW,
)
from core.process.dsl.validator import ValidationIssue
from core.tenancy.context import RequestContext

router = APIRouter(
    prefix="/process-definitions",
    tags=["process-definitions"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


class ValidationIssueResponse(BaseModel):
    field_path: str
    message: str


def _issue_response(issue: ValidationIssue) -> ValidationIssueResponse:
    return ValidationIssueResponse(field_path=issue.field_path, message=issue.message)


class ValidateRequest(BaseModel):
    definition: dict[str, object]


class ValidateResponse(BaseModel):
    valid: bool
    issues: list[ValidationIssueResponse]


@router.post("/validate")
async def validate_definition_endpoint(body: ValidateRequest) -> ValidateResponse:
    issues = validate_document(body.definition)
    return ValidateResponse(valid=not issues, issues=[_issue_response(i) for i in issues])


class CreateDefinitionRequest(BaseModel):
    key: str
    name: str
    definition: dict[str, object]
    workspace_id: uuid.UUID | None = None


class DefinitionResponse(BaseModel):
    id: uuid.UUID
    key: str
    version: int
    name: str
    workspace_id: uuid.UUID | None
    definition: dict[str, object]
    created_at: datetime


def _definition_response(row) -> DefinitionResponse:  # type: ignore[no-untyped-def]
    return DefinitionResponse(
        id=row.id,
        key=row.key,
        version=row.version,
        name=row.name,
        workspace_id=row.workspace_id,
        definition=row.definition,
        created_at=row.created_at,
    )


@router.post("", status_code=201)
async def create_definition_endpoint(
    body: CreateDefinitionRequest, ctx: RequestContext = Depends(get_request_context)
) -> DefinitionResponse:
    try:
        row = await create_definition(
            ctx.tenant_id,
            body.key,
            body.name,
            body.definition,
            workspace_id=body.workspace_id,
            created_by=ctx.principal_id,
        )
    except DefinitionValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"issues": [_issue_response(i).model_dump() for i in exc.issues]},
        ) from exc
    return _definition_response(row)


class TemplateResponse(BaseModel):
    key: str
    name: str
    definition: dict[str, object]


# Registered before "/{definition_id}" -- a literal path segment must be matched before a
# path-param route, or FastAPI tries (and fails, with a UUID-parse 422) to match "templates"
# against definition_id.
@router.get("/templates")
async def list_templates_endpoint() -> list[TemplateResponse]:
    """the template gallery: "never from an empty canvas" ( discipline 2). Serves
    its own fixtures rather than duplicating this JSON in the frontend, so the gallery
    can never drift from what the interpreter's own golden tests exercise."""
    return [
        TemplateResponse(
            key="standard_session_flow",
            name="Standard Session Flow",
            definition=STANDARD_SESSION_FLOW,
        ),
        TemplateResponse(
            key="minimal_mvp_flow", name="Minimal MVP Flow", definition=MINIMAL_MVP_FLOW
        ),
        TemplateResponse(
            key="agent_round_table",
            name="Persona Round Table",
            definition=AGENT_ROUND_TABLE_FLOW,
        ),
    ]


@router.get("/{definition_id}")
async def get_definition_endpoint(
    definition_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> DefinitionResponse:
    row = await get_definition(ctx.tenant_id, definition_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no process definition {definition_id}")
    return _definition_response(row)


@router.delete("/{definition_id}", status_code=204)
async def archive_definition_endpoint(
    definition_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> None:
    """Soft-delete (archive) a process-definition version so it leaves the list and launch
    picker. Sessions already running it keep going (they resolve it by id)."""
    await require_tenant_permission(ctx, "process_definition:archive")
    try:
        await archive_definition(ctx.tenant_id, definition_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("")
async def list_definitions_endpoint(
    workspace_id: uuid.UUID | None = None,
    ctx: RequestContext = Depends(get_request_context),
) -> list[DefinitionResponse]:
    rows = await list_definitions(ctx.tenant_id, workspace_id=workspace_id)
    return [_definition_response(r) for r in rows]
