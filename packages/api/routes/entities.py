"""Entity view endpoints (F3.10): the already-visibility-filtered read path F3.10's
React sheet renders from. Reuses ``core.assembler.visibility.scopes_for`` (the EXPORT
pseudo-phase -- "everything this principal can see in this workspace", the right
default for a standalone sheet view outside any specific process phase) and
``core.entities.injection.visible_fields`` (the same per-field ``private``-tag filter
F3.6 built for context assembly) -- one filtering rule, reused, never reimplemented for
the API. A field the viewer can't see is absent from the response entirely, never
present-with-a-blanked-value, so the client genuinely cannot tell "hidden" from
"doesn't exist" (F3.10's own acceptance criterion).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from api.model_provider_factory import get_model_provider
from core.agents.authoring import get_agent
from core.assembler.visibility import EXPORT, scopes_for
from core.entities.cel import evaluate
from core.entities.editing import (
    NoSuchSchemaError,
    apply_schema_edit_proposal,
    propose_schema_edit,
)
from core.entities.fsm import EntityStateChangeRow
from core.entities.injection import visible_fields
from core.entities.repo import (
    get_schema,
    list_latest_schemas,
    list_schema_versions,
    next_version,
    save_schema,
)
from core.entities.schema import EntitySchemaRow
from core.entities.storage import EntityRow, get_entity
from core.entities.validation import SchemaValidationError, compute_derived, validate_raw
from core.tenancy.context import RequestContext
from core.tenancy.scope import tenant_scope

router = APIRouter(
    prefix="/entities",
    tags=["entities"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


class EntityFieldView(BaseModel):
    key: str
    type: str
    value: object
    tags: list[str]
    tag_metadata: dict[str, object]
    # Server-computed for a `modifier_source`-tagged field (its `tag_metadata
    # .modifier_formula`, evaluated against the entity's own data -- same "the engine
    # computes, never the client" discipline as F3.1's derived fields). `None` for every
    # other field.
    modifier: object | None = None


def _compute_modifier(
    field_tags: list[str], tag_metadata: dict[str, object], data: dict[str, object]
) -> int | float | None:
    if "modifier_source" not in field_tags:
        return None
    formula = tag_metadata.get("modifier_formula")
    if not isinstance(formula, str):
        return None
    raw: Any = evaluate(formula, data)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return float(raw)


class EntityViewResponse(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    schema_id: uuid.UUID
    version: int
    fields: list[EntityFieldView]
    derived: dict[str, object]
    fsm_states: dict[str, str]
    views: list[dict[str, object]]


class HistoryEntryResponse(BaseModel):
    id: uuid.UUID
    field_path: str
    old_value: object | None
    new_value: object | None
    cause: str
    created_at: datetime


class EntityNotFoundError(Exception):
    pass


async def _load_entity(tenant_id: uuid.UUID, entity_id: uuid.UUID) -> EntityRow:
    entity = await get_entity(tenant_id, entity_id)
    if entity is None:
        raise EntityNotFoundError(f"no entity {entity_id}")
    return entity


class SchemaValidationIssueResponse(BaseModel):
    field_path: str
    message: str


class ValidateSchemaRequest(BaseModel):
    definition: dict[str, object]


class ValidateSchemaResponse(BaseModel):
    valid: bool
    issues: list[SchemaValidationIssueResponse]


class SchemaResponse(BaseModel):
    id: uuid.UUID
    key: str
    version: int
    workspace_id: uuid.UUID | None
    definition: dict[str, object]
    ai_assisted: bool = False


def _schema_response(row: EntitySchemaRow) -> SchemaResponse:
    return SchemaResponse(
        id=row.id,
        key=row.key,
        version=row.version,
        workspace_id=row.workspace_id,
        definition={
            "fields": row.fields,
            "derived": row.derived,
            "constraints": row.constraints,
            "state_machines": row.state_machines,
            "views": row.views,
        },
        ai_assisted=row.ai_assisted,
    )


# Registered before "/{entity_id}" -- a literal path segment must be matched before a
# path-param route, or FastAPI tries (and fails, with a UUID-parse 422) to match
# "schemas" against entity_id (same gotcha `process_definitions.py`'s own "/templates"
# route documents).
@router.post("/schemas/validate")
async def validate_schema_endpoint(body: ValidateSchemaRequest) -> ValidateSchemaResponse:
    """The schema editor's live-validate-on-keystroke call (F3.11) -- never persists
    anything, so every CEL/tag/FSM issue (including an uncompilable expression) comes
    back anchored to the exact field that produced it, the same shape B1.1's process
    editor already established for `/process-definitions/validate`."""
    _definition, issues = validate_raw(body.definition)
    return ValidateSchemaResponse(
        valid=not issues,
        issues=[
            SchemaValidationIssueResponse(field_path=i.field_path, message=i.message)
            for i in issues
        ],
    )


@router.get("/schemas/templates")
async def list_schema_templates_endpoint(
    ctx: RequestContext = Depends(get_request_context),
) -> list[SchemaResponse]:
    """F3.11's template gallery: pack-provided schemas (``workspace_id IS NULL``) --
    "new schema" always lands here first (§16.5), never an empty field list."""
    rows = await list_latest_schemas(ctx.tenant_id, None)
    return [_schema_response(r) for r in rows]


class CreateSchemaRequest(BaseModel):
    key: str
    workspace_id: uuid.UUID | None = None
    definition: dict[str, object]


@router.post("/schemas", status_code=201)
async def create_schema_endpoint(
    body: CreateSchemaRequest, ctx: RequestContext = Depends(get_request_context)
) -> SchemaResponse:
    """Creating from a template (or saving an edit) both land here: a new immutable
    version, never an in-place mutation (matching `entity_schema`'s own versioned-row
    shape, F3.1) -- entities pin whichever version id they were created against."""
    definition, issues = validate_raw(body.definition)
    if issues or definition is None:
        raise HTTPException(
            status_code=422,
            detail={
                "issues": [
                    SchemaValidationIssueResponse(
                        field_path=i.field_path, message=i.message
                    ).model_dump()
                    for i in issues
                ]
            },
        )
    version = await next_version(ctx.tenant_id, body.workspace_id, body.key)
    try:
        row = await save_schema(ctx.tenant_id, body.workspace_id, body.key, version, definition)
    except SchemaValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "issues": [
                    SchemaValidationIssueResponse(
                        field_path=i.field_path, message=i.message
                    ).model_dump()
                    for i in exc.issues
                ]
            },
        ) from exc
    return _schema_response(row)


@router.get("/schemas/versions")
async def list_schema_versions_endpoint(
    key: str,
    workspace_id: uuid.UUID | None = None,
    ctx: RequestContext = Depends(get_request_context),
) -> list[SchemaResponse]:
    """F3.11's version history panel -- every version of one key, newest first (unlike
    ``/schemas``, which collapses to the latest per key)."""
    rows = await list_schema_versions(ctx.tenant_id, workspace_id, key)
    return [_schema_response(r) for r in rows]


class ProposeSchemaEditRequest(BaseModel):
    agent_id: uuid.UUID
    instruction: str


class SchemaDefinitionDiffResponse(BaseModel):
    added_fields: list[str]
    removed_fields: list[str]
    changed_fields: list[str]
    added_derived: list[str]
    removed_derived: list[str]
    changed_derived: list[str]


class SchemaEditProposalResponse(BaseModel):
    key: str
    workspace_id: uuid.UUID | None
    proposed_definition: dict[str, object]
    diff: SchemaDefinitionDiffResponse
    valid: bool
    issues: list[SchemaValidationIssueResponse]


@router.post("/schemas/propose-edit")
async def propose_schema_edit_endpoint(
    key: str,
    workspace_id: uuid.UUID | None,
    body: ProposeSchemaEditRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> SchemaEditProposalResponse:
    """F3.12: draft-and-approve for an EntitySchema -- runs the proposed definition
    through the same ``validate_schema_definition`` pass the manual save path uses
    *before* it is ever returned, so an invalid proposal is never presented for
    approval. Approving is the separate ``POST .../apply-edit`` call below."""
    agent = await get_agent(ctx.tenant_id, body.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="no such model profile")
    try:
        proposal = await propose_schema_edit(
            ctx.tenant_id,
            workspace_id,
            key,
            body.instruction,
            agent=agent,
            provider=get_model_provider(agent.provider),
        )
    except NoSuchSchemaError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return SchemaEditProposalResponse(
        key=proposal.key,
        workspace_id=proposal.workspace_id,
        proposed_definition=proposal.proposed_definition.model_dump(mode="json"),
        diff=SchemaDefinitionDiffResponse(
            added_fields=proposal.diff.added_fields,
            removed_fields=proposal.diff.removed_fields,
            changed_fields=proposal.diff.changed_fields,
            added_derived=proposal.diff.added_derived,
            removed_derived=proposal.diff.removed_derived,
            changed_derived=proposal.diff.changed_derived,
        ),
        valid=proposal.valid,
        issues=[
            SchemaValidationIssueResponse(field_path=i.field_path, message=i.message)
            for i in proposal.issues
        ],
    )


class ApplySchemaEditRequest(BaseModel):
    key: str
    workspace_id: uuid.UUID | None = None
    proposed_definition: dict[str, object]


@router.post("/schemas/apply-edit", status_code=201)
async def apply_schema_edit_endpoint(
    body: ApplySchemaEditRequest, ctx: RequestContext = Depends(get_request_context)
) -> SchemaResponse:
    """The only write path an *approved* proposal takes -- ``save_schema`` re-validates
    independently (defense in depth), writing a new version attributed to the approving
    human and marked ``ai_assisted``."""
    definition, issues = validate_raw(body.proposed_definition)
    if issues or definition is None:
        raise HTTPException(
            status_code=422,
            detail={
                "issues": [
                    SchemaValidationIssueResponse(
                        field_path=i.field_path, message=i.message
                    ).model_dump()
                    for i in issues
                ]
            },
        )
    try:
        row = await apply_schema_edit_proposal(
            ctx.tenant_id, body.workspace_id, body.key, definition, approved_by=ctx.principal_id
        )
    except SchemaValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "issues": [
                    SchemaValidationIssueResponse(
                        field_path=i.field_path, message=i.message
                    ).model_dump()
                    for i in exc.issues
                ]
            },
        ) from exc
    return _schema_response(row)


@router.get("/schemas/{schema_id}")
async def get_schema_endpoint(
    schema_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> SchemaResponse:
    row = await get_schema(ctx.tenant_id, schema_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no schema {schema_id}")
    return _schema_response(row)


@router.get("/schemas")
async def list_schemas_endpoint(
    workspace_id: uuid.UUID | None = None,
    ctx: RequestContext = Depends(get_request_context),
) -> list[SchemaResponse]:
    rows = await list_latest_schemas(ctx.tenant_id, workspace_id)
    return [_schema_response(r) for r in rows]


class EntityListItem(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    schema_key: str
    fsm_states: dict[str, str]
    updated_at: datetime


@router.get("")
async def list_entities_endpoint(
    workspace_id: uuid.UUID,
    schema_key: str | None = None,
    ctx: RequestContext = Depends(get_request_context),
) -> list[EntityListItem]:
    """Name-level listing (no field data -- field visibility stays the per-entity
    view's job). `schema_key` filters to one schema, e.g. the swe pack's work items."""
    from core.entities.repo import get_schema as _get_schema
    from core.entities.storage import list_all_entities_for_workspace

    rows = await list_all_entities_for_workspace(ctx.tenant_id, workspace_id)
    out: list[EntityListItem] = []
    schema_keys: dict[uuid.UUID, str] = {}
    for row in rows:
        if row.schema_id not in schema_keys:
            schema_row = await _get_schema(ctx.tenant_id, row.schema_id)
            schema_keys[row.schema_id] = schema_row.key if schema_row is not None else ""
        row_schema_key = schema_keys[row.schema_id]
        if schema_key is not None and row_schema_key != schema_key:
            continue
        out.append(
            EntityListItem(
                id=row.id,
                key=row.key,
                name=row.name,
                schema_key=row_schema_key,
                fsm_states=dict(row.fsm_states),
                updated_at=row.updated_at,
            )
        )
    return sorted(out, key=lambda item: item.name)


@router.get("/{entity_id}")
async def get_entity_view_endpoint(
    entity_id: uuid.UUID,
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
) -> EntityViewResponse:
    try:
        entity = await _load_entity(ctx.tenant_id, entity_id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    schema_row = await get_schema(ctx.tenant_id, entity.schema_id)
    if schema_row is None:
        raise HTTPException(status_code=404, detail="entity's schema no longer exists")
    definition = schema_row.to_definition()

    scope_set = await scopes_for(ctx.tenant_id, ctx.principal_id, workspace_id, EXPORT, None)
    visible = visible_fields(entity, definition, frozenset(scope_set))
    derived = compute_derived(definition, entity.data)

    fields_by_key = {f.key: f for f in definition.fields}
    field_views = [
        EntityFieldView(
            key=key,
            type=fields_by_key[key].type,
            value=value,
            tags=fields_by_key[key].tags,
            tag_metadata=fields_by_key[key].tag_metadata,
            modifier=_compute_modifier(
                fields_by_key[key].tags, fields_by_key[key].tag_metadata, entity.data
            ),
        )
        for key, value in sorted(visible.items())
    ]

    return EntityViewResponse(
        id=entity.id,
        key=entity.key,
        name=entity.name,
        schema_id=entity.schema_id,
        version=entity.version,
        fields=field_views,
        derived=derived,
        fsm_states=dict(entity.fsm_states),
        views=[v.model_dump(mode="json") for v in definition.views],
    )


@router.get("/{entity_id}/history")
async def get_entity_history_endpoint(
    entity_id: uuid.UUID,
    field_path: str | None = None,
    ctx: RequestContext = Depends(get_request_context),
) -> list[HistoryEntryResponse]:
    """Per-field timeline for F3.10's history charts -- the progression chart is the
    same component/endpoint as any other numeric field's, just a different
    ``field_path``."""
    async with tenant_scope(ctx.tenant_id) as session:
        query = select(EntityStateChangeRow).where(EntityStateChangeRow.entity_id == entity_id)
        if field_path is not None:
            query = query.where(EntityStateChangeRow.field_path == field_path)
        rows = (await session.execute(query.order_by(EntityStateChangeRow.created_at))).scalars()
        return [
            HistoryEntryResponse(
                id=row.id,
                field_path=row.field_path,
                old_value=row.old_value,
                new_value=row.new_value,
                cause=row.cause,
                created_at=row.created_at,
            )
            for row in rows
        ]
