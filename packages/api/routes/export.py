"""`.pyr` export + import endpoints (G4.5/G4.6, plan §11.2/§11.4/§16.6).

Two endpoints and no more: request an export (which enqueues a job and returns its id),
and download a finished one. Deliberately *not* a synchronous `GET /export` returning a
ZIP -- a workspace with a year of sessions walks every knowledge version, every chunk
body, and every session log, which is not something an HTTP request should hold open.

The export's contents are decided entirely by `core.portability.export`, against the
calling principal's own resolved visibility. This layer chooses nothing about what goes in
the bundle; if it did, that choice would be a second visibility implementation living
where nobody would think to look for one.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from api.blob_store_factory import get_blob_store
from api.embedding_provider_factory import get_embedding_provider
from api.encryptor_factory import get_encryptor
from api.job_queue_factory import get_job_queue
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from api.moderation_provider_factory import get_moderation_provider
from api.permission_service_factory import get_permission_service
from core.portability.bundle import BundleIntegrityError
from core.portability.ccv3.export import export_agent_as_card
from core.portability.ccv3.map import import_card
from core.portability.ccv3.normalise import normalise
from core.portability.ccv3.png_chunks import (
    MalformedCardError,
    NotAPngError,
    extract_card,
)
from core.portability.crypto import WrongPasswordError
from core.portability.import_ import (
    ResolutionChainBrokenError,
    approve_quarantined_entry,
    import_bundle,
    list_quarantined_entries,
)
from core.portability.upcast import UnsupportedFormatError
from core.ports.blob_store import BlobNotFoundError
from core.ports.moderation import ModerationProvider
from core.ports.permission import PermissionService
from core.tenancy.context import RequestContext

router = APIRouter(
    prefix="/export",
    tags=["export"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


class ExportRequest(BaseModel):
    workspace_id: uuid.UUID
    # G4.7 widens this to participant | full | sanitised, with the `secret:inspect` gate
    # and audit row that `full` requires. G4.5 ships the participant path.
    mode: str = "participant"
    # Which bundle sections to include (default: everything except connections).
    # "connections" embeds model connections WITH decrypted provider credentials --
    # selecting it (or mode=full) makes `password` mandatory: plaintext export is
    # structurally unavailable for sensitive content.
    sections: list[str] | None = None
    password: str | None = None


class ExportJobResponse(BaseModel):
    job_id: uuid.UUID
    mode: str
    requested_at: str


@router.post("", status_code=202)
async def request_export(
    body: ExportRequest, ctx: RequestContext = Depends(get_request_context)
) -> ExportJobResponse:
    """The bundle is built for **the calling principal**. There is no "export as" -- an
    export is a view of the workspace through one person's visibility, and letting a
    caller name someone else would make it a way to read through their eyes."""
    from core.portability.export import DEFAULT_SECTIONS, EXPORT_SECTIONS

    sections = set(body.sections) if body.sections is not None else set(DEFAULT_SECTIONS)
    unknown = sections - EXPORT_SECTIONS
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown sections: {sorted(unknown)}")
    # The same two rules core.portability.export enforces, asked here so the user gets a
    # 422 instead of a job that fails somewhere they cannot see. The secret half calls
    # core's own counting function rather than re-deriving it: a second copy of this rule
    # is how one of them ends up wrong.
    if "connections" in sections and not body.password:
        raise HTTPException(
            status_code=422,
            detail=(
                "this export includes provider credentials -- a password is required; "
                "plaintext export is unavailable for them"
            ),
        )
    if body.mode == "full" and not body.password:
        from core.portability.export import count_guarded_secrets

        guarded = await count_guarded_secrets(ctx.tenant_id, body.workspace_id)
        if guarded:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"this workspace has {guarded} guarded secret(s) and a full export "
                    "carries their plaintext; supply a password, or mark them publishable "
                    "if they are content written to be shared"
                ),
            )
    if "connections" in sections:
        # embedding credentials is a tenant-management act, not a workspace read
        from api.authz import require_tenant_permission

        await require_tenant_permission(ctx, "manage_tenant")

    requested_at = datetime.now(UTC).isoformat()
    job_id = await get_job_queue().enqueue(
        ctx.tenant_id,
        "export_workspace",
        {
            "tenant_id": str(ctx.tenant_id),
            "workspace_id": str(body.workspace_id),
            "viewer_principal_id": str(ctx.principal_id),
            "mode": body.mode,
            "sections": sorted(sections),
            # sealed for the queue hop -- a job row must not hold the password in
            # the clear (the whole point of the password is people other than the
            # exporter can read the database)
            "password_sealed": (get_encryptor().encrypt(body.password) if body.password else None),
            "requested_at": requested_at,
        },
    )
    return ExportJobResponse(job_id=job_id, mode=body.mode, requested_at=requested_at)


@router.get("/{job_id}/download")
async def download_export(
    job_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> Response:
    """Streams the finished bundle. The blob key is read off the *job*, never accepted
    from the caller -- a caller-supplied key would turn this into a way to read any blob
    the deployment happens to hold."""
    job = await get_job_queue().get(job_id)
    # The job table carries no RLS (core.ports.job_queue), so the tenant check here is the
    # only thing between this endpoint and another tenant's export.
    if job is None or job.tenant_id != ctx.tenant_id or job.kind != "export_workspace":
        raise HTTPException(status_code=404, detail=f"no export job {job_id} in this tenant")
    # The queue's terminal status is "done" (adapters/queue/postgres); "completed" here
    # made every download 409 forever.
    if job.status != "done" or not job.result:
        raise HTTPException(status_code=409, detail=f"export job {job_id} is {job.status}")
    if str(job.payload.get("viewer_principal_id")) != str(ctx.principal_id):
        raise HTTPException(
            status_code=403,
            detail="an export is built through one principal's visibility; only that "
            "principal may download it",
        )

    blob_key = str(job.result["blob_key"])
    try:
        data = await get_blob_store().get(blob_key)
    except BlobNotFoundError as exc:
        raise HTTPException(status_code=410, detail=f"export blob {blob_key} is gone") from exc

    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="export-{job_id}.pyr"'},
    )


# ── G4.6: import ────────────────────────────────────────────────────────────────────


class ImportResponse(BaseModel):
    knowledge_sources: int
    entries: int
    sessions: int
    resolutions: int
    quarantined: list[dict[str, str]]
    forked_keys: list[dict[str, str]]
    # What else came, and what did not. A bundle's flow, vocabulary, personas and secrets
    # either arrive or are named here -- an import that quietly drops half a workspace is
    # the failure this reports its way out of.
    imported: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []


class QuarantinedEntryResponse(BaseModel):
    id: uuid.UUID
    entry_key: str
    title: str
    reason: str | None


class BundleItemResponse(BaseModel):
    key: str
    name: str
    # Already present in this tenant. Importing anyway is safe -- the incoming object
    # forks to `<key>-imported` and nothing resident is touched -- but it is how a
    # workspace doubles on a second import, so it is said before rather than after.
    collides: bool


class BundleInspectionResponse(BaseModel):
    tenant_ref: str
    workflow_key: str
    app_version: str
    exported_at: str
    encrypted: bool
    collisions: int
    compatibility: str
    compatibility_note: str
    sections: dict[str, list[BundleItemResponse]]


@router.post("/import/inspect")
async def inspect_bundle_endpoint(
    file: UploadFile = File(...),
    password: str | None = Form(default=None),
    ctx: RequestContext = Depends(get_request_context),
) -> BundleInspectionResponse:
    """What is in this file, and what of it is already here. Writes nothing.

    The bundle is opened and its integrity map verified exactly as the import does:
    describing a file the importer would then refuse is worse than refusing now."""
    from core.portability.inspect import inspect_bundle

    data = await file.read()
    try:
        found = await inspect_bundle(
            data, ctx.tenant_id, password=password, encryptor=get_encryptor()
        )
    except WrongPasswordError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except BundleIntegrityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UnsupportedFormatError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    return BundleInspectionResponse(
        tenant_ref=found.tenant_ref,
        workflow_key=found.workflow_key,
        app_version=found.app_version,
        exported_at=found.exported_at,
        encrypted=found.encrypted,
        collisions=found.collisions,
        compatibility=found.compatibility,
        compatibility_note=found.compatibility_note,
        sections={
            name: [BundleItemResponse(key=i.key, name=i.name, collides=i.collides) for i in items]
            for name, items in found.sections.items()
        },
    )


@router.post("/import", status_code=201)
async def import_bundle_endpoint(
    workspace_id: uuid.UUID,
    file: UploadFile = File(...),
    password: str | None = Form(default=None),
    # Comma-separated section names (see core.portability.inspect.SECTIONS). Absent means
    # all of them, which is what every caller before this meant and still means.
    #
    # An *empty* value is indistinguishable from absent here -- a multipart field with no
    # content does not reach the handler -- so it also means everything. That is the wrong
    # way round for a field whose empty state reads as "none of it", which is why the UI
    # refuses to submit an empty selection rather than sending one.
    sections: str | None = Form(default=None),
    ctx: RequestContext = Depends(get_request_context),
    permission_service: PermissionService = Depends(get_permission_service),
    moderation_provider: ModerationProvider = Depends(get_moderation_provider),
) -> ImportResponse:
    """Synchronous, unlike export -- and deliberately so. An import either verifies and
    lands or is refused, and the caller needs that answer (with the *location* of a broken
    resolution chain) in the response, not in a job they have to go poll for.

    The ingestion disclaimer §16.6 asks for is the UI's job, not this route's: it is
    posture shown before a user chooses to import, whereas the envelope and the scanner
    are the mitigations, and putting the disclaimer text here would imply otherwise."""
    data = await file.read()
    chosen = (
        frozenset(s.strip() for s in sections.split(",") if s.strip())
        if sections is not None
        else None
    )
    try:
        # Shielded: the importer commits section by section, so a client disconnect
        # mid-flight (an impatient proxy, a first boot still warming the embedding
        # model) would otherwise cancel the task BETWEEN transactions and leave a
        # half-imported workspace with no error -- exactly the "lands or is refused"
        # promise this route makes. Once started, the import runs to completion; a
        # client that gave up early merely misses the response of a successful import.
        report = await asyncio.shield(
            import_bundle(
                data,
                ctx.tenant_id,
                workspace_id,
                bundle_ref=file.filename or "uploaded.pyr",
                password=password,
                encryptor=get_encryptor(),
                # Secrets are authored *as the importer*: they must be entitled to author
                # in this workspace, and imported content is moderated like any other.
                importing_principal_id=ctx.principal_id,
                permission_service=permission_service,
                moderation_provider=moderation_provider,
                # Without this every imported secret is invisible to the disclosure gate.
                embedding_provider=get_embedding_provider(),
                sections=chosen,
            )
        )
    except WrongPasswordError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except BundleIntegrityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ResolutionChainBrokenError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "resolution_chain_broken",
                "session_ref": exc.session_ref,
                "event_seq": exc.event_seq,
                "detail": exc.detail,
            },
        ) from exc
    except UnsupportedFormatError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    return ImportResponse(
        knowledge_sources=report.knowledge_sources,
        entries=report.entries,
        sessions=report.sessions,
        resolutions=report.resolutions,
        quarantined=[{"entry_key": k, "reason": r} for k, r in report.quarantined_entries],
        forked_keys=[{"from": a, "to": b} for a, b in report.forked_keys],
        imported=report.imported,
        skipped=report.skipped,
        warnings=report.warnings,
    )


@router.get("/quarantine")
async def list_quarantine(
    ctx: RequestContext = Depends(get_request_context),
) -> list[QuarantinedEntryResponse]:
    """The review queue behind §16.6's "human reviews each in a dedicated UI"."""
    entries = await list_quarantined_entries(ctx.tenant_id)
    return [
        QuarantinedEntryResponse(
            id=e.id, entry_key=e.entry_key, title=e.title, reason=e.quarantine_reason
        )
        for e in entries
    ]


@router.post("/quarantine/{entry_id}/approve", status_code=204)
async def approve_quarantine(
    entry_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> Response:
    """Clearing quarantine is a *human* act, which is why there is no bulk endpoint: an
    "approve all" button is how a review queue becomes a formality."""
    try:
        await approve_quarantined_entry(ctx.tenant_id, entry_id, ctx.principal_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=204)


# ── G4.8: CCv2/CCv3 card import ─────────────────────────────────────────────────────


class CardImportResponse(BaseModel):
    persona_id: uuid.UUID
    knowledge_source_id: uuid.UUID | None
    spec_version: str
    entry_keys: list[str]
    quarantined: list[dict[str, str]]
    embed_job_id: uuid.UUID | None


@router.post("/cards/import", status_code=201)
async def import_card_endpoint(
    workspace_id: uuid.UUID,
    file: UploadFile = File(...),
    ctx: RequestContext = Depends(get_request_context),
) -> CardImportResponse:
    """The ecosystem's front door (§11.3): a PNG card, or the plain JSON some tools share
    instead. Both spec versions land on the same internal shape before anything is written.

    Embedding is enqueued rather than done here. Until it runs the imported lore still
    activates through the keyed path -- which is how lore is meant to fire anyway -- so the
    card is playable the moment this returns rather than after a queue drains."""
    data = await file.read()
    try:
        keyword, payload = extract_card(data)
        card = normalise(keyword, payload)
    except (NotAPngError, MalformedCardError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        result = await import_card(card, ctx.tenant_id, workspace_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    embed_job_id = None
    if result.knowledge_source_id is not None:
        embed_job_id = await get_job_queue().enqueue(
            ctx.tenant_id,
            "embed_chunks",
            {
                "tenant_id": str(ctx.tenant_id),
                "knowledge_source_id": str(result.knowledge_source_id),
            },
        )

    return CardImportResponse(
        persona_id=result.persona_id,
        knowledge_source_id=result.knowledge_source_id,
        spec_version=card.spec_version,
        entry_keys=result.entry_keys,
        quarantined=[{"entry_key": k, "reason": r} for k, r in result.quarantined],
        embed_job_id=embed_job_id,
    )


# ── G4.9: CCv3 card export ──────────────────────────────────────────────────────────


class LossItemResponse(BaseModel):
    kind: str
    detail: str
    carried_in_extension: bool


class CardExportPreviewResponse(BaseModel):
    """The loss report, *before* download. Its own endpoint rather than a header on the
    download, because §11.3 requires the user to see what a card cannot carry and then
    decide -- and a report delivered alongside the file has already lost that argument."""

    persona_id: uuid.UUID
    name: str
    entry_count: int
    loss_report: list[LossItemResponse]
    rendered: str


@router.get("/cards/{persona_id}/preview")
async def preview_card_export(
    persona_id: uuid.UUID,
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
) -> CardExportPreviewResponse:
    try:
        result = await export_agent_as_card(ctx.tenant_id, workspace_id, persona_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CardExportPreviewResponse(
        persona_id=persona_id,
        name=str(result.payload["data"]["name"]),
        entry_count=len(result.payload["data"]["character_book"]["entries"]),
        loss_report=[LossItemResponse(**item) for item in result.loss_report.to_json()],
        rendered=result.loss_report.render(),
    )


@router.post("/cards/{persona_id}")
async def export_card(
    persona_id: uuid.UUID,
    workspace_id: uuid.UUID,
    image: UploadFile = File(...),
    ctx: RequestContext = Depends(get_request_context),
) -> Response:
    """The caller supplies the PNG the card is written into. Pyrrhula does not generate
    portrait art and should not pretend to: a card's image is the author's, and shipping a
    placeholder would put a Pyrrhula-branded square on every card anyone exports."""
    try:
        result = await export_agent_as_card(ctx.tenant_id, workspace_id, persona_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        png = result.to_png(await image.read())
    except NotAPngError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Content-Disposition": f'attachment; filename="{persona_id}.card.png"',
            # The loss report travels with the bytes too, so a scripted export cannot end
            # up quieter about it than the UI is.
            "X-Pyrrhula-Loss-Items": str(len(result.loss_report.items)),
        },
    )
