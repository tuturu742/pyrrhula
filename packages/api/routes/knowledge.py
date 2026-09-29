"""Knowledge authoring endpoints: create sources, edit draft entries, publish
immutable versions, attach sources to workspaces. Talks to
``core.knowledge.authoring`` — never ``core.knowledge.repo``, which INV-1 reserves for
``core.assembler``/``core.overseer`` (see that module's docstring for why authoring is
exempt). This is the CRUD surface the authoring UX (diffs, re-splitting, bulk import)
builds on.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from api.authz import require_tenant_permission
from api.blob_store_factory import get_blob_store
from api.job_queue_factory import get_job_queue
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from api.model_provider_factory import get_model_provider
from core.agents.authoring import get_agent
from core.knowledge.authoring import (
    EntryFields,
    archive_source,
    attach_source_to_workspace,
    create_source,
    list_draft_entries,
    list_source_attachments,
    list_sources,
    list_version_entries,
    list_versions,
    list_workspace_attachments,
    publish_version,
    upsert_draft_entry,
)
from core.knowledge.diff import diff_versions
from core.knowledge.editing import (
    NoSuchDraftEntryError,
    apply_knowledge_edit_proposal,
    propose_knowledge_edit,
)
from core.knowledge.library import LIBRARY_TENANT_ID, fork_if_library
from core.knowledge.versioning import fork_source, resolve_effective_version_id
from core.tenancy.context import RequestContext

router = APIRouter(
    prefix="/knowledge",
    tags=["knowledge"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


class CreateSourceRequest(BaseModel):
    key: str
    name: str
    class_: str = Field(alias="class")
    visibility: str = "tenant"

    model_config = {"populate_by_name": True}


class SourceResponse(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    class_: str = Field(serialization_alias="class")
    visibility: str
    current_version_id: uuid.UUID | None
    # a library-tenant source is visible under every tenant's scoped
    # session via the RLS disjunct, but only the library tenant itself can ever write one
    # -- the UI badges it read-only and routes edits through fork-on-edit
    # (upsert_draft_entry_endpoint already does this server-side regardless of what the
    # client sends; this field is purely so the UI can *show* that before the user tries).
    is_library: bool

    model_config = {"populate_by_name": True}


def _source_response(source) -> SourceResponse:  # type: ignore[no-untyped-def]
    return SourceResponse(
        id=source.id,
        key=source.key,
        name=source.name,
        class_=source.class_,
        visibility=source.visibility,
        current_version_id=source.current_version_id,
        is_library=source.tenant_id == LIBRARY_TENANT_ID,
    )


@router.post("/sources", status_code=201)
async def create_source_endpoint(
    body: CreateSourceRequest, ctx: RequestContext = Depends(get_request_context)
) -> SourceResponse:
    source = await create_source(
        ctx.tenant_id, body.key, body.name, body.class_, visibility=body.visibility
    )
    return _source_response(source)


@router.get("/sources")
async def list_sources_endpoint(
    ctx: RequestContext = Depends(get_request_context),
) -> list[SourceResponse]:
    sources = await list_sources(ctx.tenant_id)
    return [_source_response(s) for s in sources]


@router.delete("/sources/{source_id}", status_code=204)
async def archive_source_endpoint(
    source_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> None:
    """Soft-delete (archive) a knowledge source and detach it from workspaces, so retrieval
    drops it. Its append-only version history stays intact; purge is the superuser CLI."""
    await require_tenant_permission(ctx, "knowledge:archive")
    try:
        await archive_source(ctx.tenant_id, source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class EntryRequest(BaseModel):
    title: str
    body_md: str
    class_: str = Field(alias="class")
    scope_key: str
    keys: list[str] = []
    secondary_keys: list[str] = []
    logic: str = "AND"
    use_regex: bool = False
    constant: bool = False
    sticky: int | None = None
    cooldown: int | None = None
    delay: int | None = None
    trigger_pct: int | None = None
    inclusion_group: str | None = None
    position: str = "before_char"
    insertion_order: int = 0

    model_config = {"populate_by_name": True}


class EntryResponse(BaseModel):
    id: uuid.UUID
    entry_key: str
    title: str
    body_md: str
    class_: str = Field(serialization_alias="class")
    scope_key: str
    version_id: uuid.UUID | None
    # the activation fields -- EntryRequest already accepted these on
    # write; the response was missing them, which made an entry editor unable to
    # round-trip an *existing* entry's activation state without a second, ad hoc read
    # path. Same field set, same defaults, symmetric with EntryRequest.
    keys: list[str] = []
    # Whether those keys came from the entry's title rather than from a person, so the
    # editor can say so -- an author reading keys they did not write should be told.
    keys_derived: bool = False
    secondary_keys: list[str] = []
    logic: str = "AND"
    use_regex: bool = False
    constant: bool = False
    sticky: int | None = None
    cooldown: int | None = None
    delay: int | None = None
    trigger_pct: int | None = None
    inclusion_group: str | None = None
    position: str = "before_char"
    insertion_order: int = 0
    # set only when this write was redirected into a fork-on-edit copy -- i.e.
    # ``source_id`` in the request path named a library source. The caller's next request
    # should address the fork, not the (untouched, still-library-owned) original.
    forked_source_id: uuid.UUID | None = None

    model_config = {"populate_by_name": True}


def _entry_response(entry, *, forked_source_id: uuid.UUID | None = None) -> EntryResponse:  # type: ignore[no-untyped-def]
    return EntryResponse(
        id=entry.id,
        entry_key=entry.entry_key,
        title=entry.title,
        body_md=entry.body_md,
        class_=entry.class_,
        scope_key=entry.scope_key,
        version_id=entry.version_id,
        keys=list(entry.keys),
        keys_derived=bool(getattr(entry, "keys_derived", False)),
        secondary_keys=list(entry.secondary_keys),
        logic=entry.logic,
        use_regex=entry.use_regex,
        constant=entry.constant,
        sticky=entry.sticky,
        cooldown=entry.cooldown,
        delay=entry.delay,
        trigger_pct=entry.trigger_pct,
        inclusion_group=entry.inclusion_group,
        position=entry.position,
        insertion_order=entry.insertion_order,
        forked_source_id=forked_source_id,
    )


@router.put("/sources/{source_id}/entries/{entry_key}", status_code=200)
async def upsert_draft_entry_endpoint(
    source_id: uuid.UUID,
    entry_key: str,
    body: EntryRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> EntryResponse:
    try:
        effective_source_id = await fork_if_library(
            ctx.tenant_id, source_id, created_by=ctx.principal_id
        )
        entry = await upsert_draft_entry(
            ctx.tenant_id,
            effective_source_id,
            entry_key,
            EntryFields(
                title=body.title,
                body_md=body.body_md,
                class_=body.class_,
                scope_key=body.scope_key,
                keys=body.keys,
                secondary_keys=body.secondary_keys,
                logic=body.logic,
                use_regex=body.use_regex,
                constant=body.constant,
                sticky=body.sticky,
                cooldown=body.cooldown,
                delay=body.delay,
                trigger_pct=body.trigger_pct,
                inclusion_group=body.inclusion_group,
                position=body.position,
                insertion_order=body.insertion_order,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    forked_source_id = effective_source_id if effective_source_id != source_id else None
    return _entry_response(entry, forked_source_id=forked_source_id)


@router.get("/sources/{source_id}/entries")
async def list_draft_entries_endpoint(
    source_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> list[EntryResponse]:
    entries = await list_draft_entries(ctx.tenant_id, source_id)
    return [_entry_response(e) for e in entries]


@router.get("/sources/{source_id}/versions/{version_id}/entries")
async def list_version_entries_endpoint(
    source_id: uuid.UUID,
    version_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
) -> list[EntryResponse]:
    entries = await list_version_entries(ctx.tenant_id, version_id)
    return [_entry_response(e) for e in entries]


class PublishRequest(BaseModel):
    change_note: str | None = None


class VersionResponse(BaseModel):
    id: uuid.UUID
    version_number: int
    content_hash: str
    parent_version_id: uuid.UUID | None
    change_note: str | None = None
    created_at: str | None = None
    ai_assisted: bool = False


def _version_response(version) -> VersionResponse:  # type: ignore[no-untyped-def]
    return VersionResponse(
        id=version.id,
        version_number=version.version_number,
        content_hash=version.content_hash,
        parent_version_id=version.parent_version_id,
        change_note=version.change_note,
        created_at=version.created_at.isoformat() if version.created_at else None,
        ai_assisted=version.ai_assisted,
    )


@router.post("/sources/{source_id}/publish", status_code=201)
async def publish_version_endpoint(
    source_id: uuid.UUID,
    body: PublishRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> VersionResponse:
    try:
        version = await publish_version(
            ctx.tenant_id, source_id, created_by=ctx.principal_id, change_note=body.change_note
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await _enqueue_embedding(ctx.tenant_id, source_id)
    return _version_response(version)


async def _enqueue_embedding(tenant_id: uuid.UUID, source_id: uuid.UUID) -> None:
    """Publishing chunks the new version (core); the vectors are the worker's job. Until
    it runs, the new chunks answer lexical search only -- and without this call they
    never answered semantic search at all."""
    await get_job_queue().enqueue(
        tenant_id,
        "embed_chunks",
        {"tenant_id": str(tenant_id), "knowledge_source_id": str(source_id)},
    )


class ProposeEntryEditRequest(BaseModel):
    agent_id: uuid.UUID
    instruction: str


class KnowledgeEditProposalResponse(BaseModel):
    entry_key: str
    current_body_md: str
    proposed_body_md: str
    text_diff: str
    valid: bool
    issues: list[str]


class ApplyEntryEditRequest(BaseModel):
    proposed_body_md: str


@router.post("/sources/{source_id}/entries/{entry_key}/propose-edit")
async def propose_entry_edit_endpoint(
    source_id: uuid.UUID,
    entry_key: str,
    body: ProposeEntryEditRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> KnowledgeEditProposalResponse:
    """draft-and-approve for a knowledge entry's body -- this endpoint only ever
    proposes. Approving is the separate ``POST .../apply-edit`` call below, which is what
    actually publishes a new version."""
    agent = await get_agent(ctx.tenant_id, body.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="no such model profile")
    try:
        proposal = await propose_knowledge_edit(
            ctx.tenant_id,
            source_id,
            entry_key,
            body.instruction,
            agent=agent,
            provider=get_model_provider(agent.provider),
        )
    except NoSuchDraftEntryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return KnowledgeEditProposalResponse(
        entry_key=proposal.entry_key,
        current_body_md=proposal.current_body_md,
        proposed_body_md=proposal.proposed_body_md,
        text_diff=proposal.text_diff,
        valid=proposal.valid,
        issues=proposal.issues,
    )


@router.post("/sources/{source_id}/entries/{entry_key}/apply-edit", status_code=201)
async def apply_entry_edit_endpoint(
    source_id: uuid.UUID,
    entry_key: str,
    body: ApplyEntryEditRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> VersionResponse:
    """The only write path an *approved* proposal takes: folds the proposed body into
    the draft and publishes -- a new immutable version, attributed to the approving
    human, marked ``ai_assisted``."""
    try:
        version = await apply_knowledge_edit_proposal(
            ctx.tenant_id, source_id, entry_key, body.proposed_body_md, approved_by=ctx.principal_id
        )
    except NoSuchDraftEntryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await _enqueue_embedding(ctx.tenant_id, source_id)
    return _version_response(version)


@router.get("/sources/{source_id}/versions")
async def list_versions_endpoint(
    source_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> list[VersionResponse]:
    """The version history panel -- newest first (``list_versions``'s own ordering)."""
    versions = await list_versions(ctx.tenant_id, source_id)
    return [_version_response(v) for v in versions]


class AttachSourceRequest(BaseModel):
    workspace_id: uuid.UUID
    scope_key: str
    priority_weight: float = 1.0
    version_pin: uuid.UUID | None = None
    enabled: bool = True


class AttachmentResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    knowledge_source_id: uuid.UUID
    scope_key: str
    priority_weight: float
    version_pin: uuid.UUID | None
    enabled: bool


def _attachment_response(attachment) -> AttachmentResponse:  # type: ignore[no-untyped-def]
    return AttachmentResponse(
        id=attachment.id,
        workspace_id=attachment.workspace_id,
        knowledge_source_id=attachment.knowledge_source_id,
        scope_key=attachment.scope_key,
        priority_weight=float(attachment.priority_weight),
        version_pin=attachment.version_pin,
        enabled=attachment.enabled,
    )


@router.post("/sources/{source_id}/attachments", status_code=201)
async def attach_source_endpoint(
    source_id: uuid.UUID,
    body: AttachSourceRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> AttachmentResponse:
    try:
        attachment = await attach_source_to_workspace(
            ctx.tenant_id,
            body.workspace_id,
            source_id,
            body.scope_key,
            priority_weight=body.priority_weight,
            version_pin=body.version_pin,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _attachment_response(attachment)


@router.get("/workspaces/{workspace_id}/attachments")
async def list_workspace_attachments_endpoint(
    workspace_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> list[AttachmentResponse]:
    attachments = await list_workspace_attachments(ctx.tenant_id, workspace_id)
    return [_attachment_response(a) for a in attachments]


class ClassSaturationResponse(BaseModel):
    class_: str = Field(serialization_alias="class")
    bucket_tokens: int
    constant_tokens: int
    retrievable_tokens: int
    constant_entries: int
    # How many of them are guaranteed a place, and how many therefore are not: the share
    # cap keeps room for retrieval, and an author with more always-on text than fits
    # should be told which way that went.
    admitted_constant_entries: int
    admitted_constant_tokens: int
    dropped_constant_entries: int
    # Every always-on entry of this class, in the order the budget offers them (the
    # author's insertion_order). The ones past `admitted_constant_entries` are the ones
    # that only appear when retrieval leaves room.
    constant_entry_keys: list[str]
    saturated: bool
    tight: bool


class PhaseSaturationResponse(BaseModel):
    phase_key: str
    max_tokens: int
    history_reserved_tokens: int
    classes: list[ClassSaturationResponse]


class SaturationResponse(BaseModel):
    process_definition_id: uuid.UUID
    phases: list[PhaseSaturationResponse]


@router.get("/workspaces/{workspace_id}/saturation")
async def workspace_saturation_endpoint(
    workspace_id: uuid.UUID,
    process_definition_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
) -> SaturationResponse:
    """Does this workspace's knowledge have room to be retrieved under this flow?

    Per phase and class: the bucket the phase gives it, and what the always-on entries
    already occupy. A saturated class is one whose constants fill the bucket, which turns
    retrieval for that class off -- silently, because a full bucket is what a bucket is
    for. Read-only arithmetic over attachments, entries and the flow."""
    from core.knowledge.retrieval.saturation import workspace_saturation
    from core.process.authoring import get_definition
    from core.process.dsl.validator import validate_raw

    row = await get_definition(ctx.tenant_id, process_definition_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no flow {process_definition_id}")
    dsl, issues = validate_raw(row.definition)
    if dsl is None:
        raise HTTPException(
            status_code=422, detail=f"flow does not validate: {issues[0].message if issues else ''}"
        )
    report = await workspace_saturation(ctx.tenant_id, workspace_id, dsl)
    return SaturationResponse(
        process_definition_id=process_definition_id,
        phases=[
            PhaseSaturationResponse(
                phase_key=phase.phase_key,
                max_tokens=phase.max_tokens,
                history_reserved_tokens=phase.history_reserved_tokens,
                classes=[
                    ClassSaturationResponse(
                        class_=c.class_,
                        bucket_tokens=c.bucket_tokens,
                        constant_tokens=c.constant_tokens,
                        retrievable_tokens=c.retrievable_tokens,
                        constant_entries=c.constant_entries,
                        admitted_constant_entries=c.admitted_constant_entries,
                        admitted_constant_tokens=c.admitted_constant_tokens,
                        dropped_constant_entries=c.dropped_constant_entries,
                        constant_entry_keys=c.constant_entry_keys,
                        saturated=c.saturated,
                        tight=c.tight,
                    )
                    for c in phase.classes
                ],
            )
            for phase in report
        ],
    )


@router.get("/sources/{source_id}/attachments")
async def list_source_attachments_endpoint(
    source_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> list[AttachmentResponse]:
    """The source detail page: which workspaces is *this* source attached to."""
    attachments = await list_source_attachments(ctx.tenant_id, source_id)
    return [_attachment_response(a) for a in attachments]


class IngestJobResponse(BaseModel):
    job_id: uuid.UUID


@router.post("/sources/{source_id}/ingest", status_code=202)
async def ingest_document_endpoint(
    source_id: uuid.UUID,
    file: UploadFile = File(...),
    class_: str = Form(alias="class"),
    scope_key: str = Form(),
    ctx: RequestContext = Depends(get_request_context),
) -> IngestJobResponse:
    """Stores the upload and enqueues a worker job — parsing/chunking never happens in
    this process ("a 200-page PDF ingests without blocking the API"), only a blob
    write and a job insert, both fast regardless of document size."""
    data = await file.read()
    blob_key = f"knowledge/{ctx.tenant_id}/{source_id}/{uuid.uuid4()}-{file.filename}"
    await get_blob_store().put(blob_key, data, content_type=file.content_type)

    job_id = await get_job_queue().enqueue(
        ctx.tenant_id,
        "knowledge_ingest",
        {
            "tenant_id": str(ctx.tenant_id),
            "knowledge_source_id": str(source_id),
            "blob_key": blob_key,
            "filename": file.filename,
            "class": class_,
            "scope_key": scope_key,
        },
    )
    return IngestJobResponse(job_id=job_id)


class JobStatusResponse(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    result: dict[str, object] | None
    error: str | None


@router.get("/jobs/{job_id}")
async def get_ingest_job_endpoint(
    job_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> JobStatusResponse:
    job = await get_job_queue().get(job_id)
    # The job table carries no RLS (core.ports.job_queue) -- a manual tenant check here
    # is the only thing standing between this endpoint and reading another tenant's job.
    if job is None or job.tenant_id != ctx.tenant_id:
        raise HTTPException(status_code=404, detail=f"no job {job_id} in this tenant")
    return JobStatusResponse(
        id=job.id, kind=job.kind, status=job.status, result=job.result, error=job.error
    )


@router.post("/reembed", status_code=202)
async def reembed_stale_endpoint(
    ctx: RequestContext = Depends(get_request_context),
) -> IngestJobResponse:
    """sweep every chunk for this tenant whose ``embedding_model`` doesn't match
    the currently configured one (an operator changed the embedding model/tag) and
    re-embed it, reusing cached vectors by content_hash where possible."""
    job_id = await get_job_queue().enqueue(
        ctx.tenant_id, "reembed_stale", {"tenant_id": str(ctx.tenant_id)}
    )
    return IngestJobResponse(job_id=job_id)


class ChangedEntryResponse(BaseModel):
    entry_key: str
    text_diff: str


class VersionDiffResponse(BaseModel):
    added: list[str]
    removed: list[str]
    changed: list[ChangedEntryResponse]


@router.get("/sources/{source_id}/diff")
async def diff_versions_endpoint(
    source_id: uuid.UUID,
    from_version_id: uuid.UUID,
    to_version_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
) -> VersionDiffResponse:
    """``source_id`` isn't used to scope the diff query itself (versions are looked up
    directly by id, tenant-scoped) -- it's in the path for a stable, RESTful URL shape;
    a version id from a different source in the same tenant would just diff against
    itself, an odd but harmless request, not a cross-tenant leak."""
    diff = await diff_versions(ctx.tenant_id, from_version_id, to_version_id)
    return VersionDiffResponse(
        added=diff.added,
        removed=diff.removed,
        changed=[
            ChangedEntryResponse(entry_key=c.entry_key, text_diff=c.text_diff) for c in diff.changed
        ],
    )


class ForkSourceRequest(BaseModel):
    from_version_id: uuid.UUID
    new_key: str
    new_name: str


@router.post("/sources/{source_id}/fork", status_code=201)
async def fork_source_endpoint(
    source_id: uuid.UUID,
    body: ForkSourceRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> SourceResponse:
    try:
        forked = await fork_source(
            ctx.tenant_id,
            source_id,
            body.from_version_id,
            body.new_key,
            body.new_name,
            created_by=ctx.principal_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await _enqueue_embedding(ctx.tenant_id, forked.id)
    return _source_response(forked)


class EffectiveVersionResponse(BaseModel):
    version_id: uuid.UUID | None


@router.get("/workspaces/{workspace_id}/sources/{source_id}/effective-version")
async def effective_version_endpoint(
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
) -> EffectiveVersionResponse:
    version_id = await resolve_effective_version_id(ctx.tenant_id, workspace_id, source_id)
    return EffectiveVersionResponse(version_id=version_id)
