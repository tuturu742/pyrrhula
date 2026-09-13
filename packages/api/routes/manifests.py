"""D1.4: per-message ContextManifest inspection (C1.3, plan §15.4 -- "a differentiator,
not a debug tool"). What was retrieved, which bucket, what rank, why, and what it cost --
read straight from the durably-written manifest (INV-10), never recomputed. Access
control is C1.3's own ``get_manifest_for_message``: the exact viewer of a manifest may
always read it back; anyone else needs ``read_any_manifest`` on the workspace.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from api.permission_service_factory import get_permission_service
from core.assembler.manifest import (
    ManifestAccessDeniedError,
    ManifestNotFoundError,
    get_manifest_for_message,
)
from core.assembler.models import ContextManifestRow
from core.sessions.models import MessageRow, SessionRow
from core.tenancy.context import RequestContext
from core.tenancy.scope import tenant_scope

router = APIRouter(
    tags=["manifests"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


class ManifestEntryResponse(BaseModel):
    citation_id: str
    chunk_id: str
    entry_id: str
    entry_key: str
    source_id: str
    version_id: str | None
    class_: str = Field(serialization_alias="class")
    bucket: str
    rank: int
    score: float
    why: str
    token_count: int


class RedactionResponse(BaseModel):
    type: str
    id: str
    reason: str


class ManifestResponse(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    event_seq: int
    viewer_principal_id: uuid.UUID
    phase: str
    entries: list[ManifestEntryResponse]
    redactions: list[RedactionResponse]
    resolution_ids: list[str]
    token_counts: dict[str, object]
    rendered_hash: str


def _manifest_response(row: ContextManifestRow) -> ManifestResponse:
    return ManifestResponse(
        id=row.id,
        session_id=row.session_id,
        event_seq=row.event_seq,
        viewer_principal_id=row.viewer_principal_id,
        phase=row.phase,
        entries=[
            ManifestEntryResponse(
                citation_id=str(e["citation_id"]),
                chunk_id=str(e["chunk_id"]),
                entry_id=str(e["entry_id"]),
                entry_key=str(e["entry_key"]),
                source_id=str(e["source_id"]),
                version_id=str(e["version_id"]) if e.get("version_id") else None,
                class_=str(e["class"]),
                bucket=str(e["bucket"]),
                rank=int(e["rank"]),  # type: ignore[call-overload]
                score=float(e["score"]),  # type: ignore[arg-type]
                why=str(e["why"]),
                token_count=int(e["token_count"]),  # type: ignore[call-overload]
            )
            for e in row.entries
        ],
        redactions=[
            RedactionResponse(type=str(r["type"]), id=str(r["id"]), reason=str(r["reason"]))
            for r in row.redactions
        ],
        resolution_ids=[str(r) for r in row.resolution_ids],
        token_counts=dict(row.token_counts),
        rendered_hash=row.rendered_hash,
    )


@router.get("/messages/{message_id}/manifest")
async def get_message_manifest(
    message_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> ManifestResponse:
    async with tenant_scope(ctx.tenant_id) as session:
        message = await session.get(MessageRow, message_id)
        if message is None:
            raise HTTPException(status_code=404, detail=f"no message {message_id}")
        sess = await session.get(SessionRow, message.session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail=f"no message {message_id}")
        workspace_id = sess.workspace_id

    try:
        manifest_row = await get_manifest_for_message(
            ctx.tenant_id,
            workspace_id,
            message_id,
            ctx.principal_id,
            permission_service=get_permission_service(),
        )
    except ManifestNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ManifestAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return _manifest_response(manifest_row)
