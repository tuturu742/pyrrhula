"""Citation resolution: resolve a message's validated citations to
their pinned-version entry content -- the UI renders these as links that stay correct
even after the rulebook changes.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from core.assembler.citations import resolve_citation
from core.sessions.models import MessageRow
from core.tenancy.context import RequestContext
from core.tenancy.scope import tenant_scope

router = APIRouter(
    tags=["citations"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


class ResolvedCitationResponse(BaseModel):
    citation_id: str
    entry_key: str
    source_id: uuid.UUID
    version_id: uuid.UUID | None
    title: str
    body_md: str


class MessageCitationsResponse(BaseModel):
    valid: list[ResolvedCitationResponse]
    # cited ids the reply used that weren't actually present in the manifest that
    # produced its context (core.assembler.citations.apply_citation_validation writes
    # these onto message.moderation_flags.bad_citation) -- the inspector's hallucinated-
    # citation flag reads straight from here, never re-derived from the reply text.
    bad_citation_ids: list[str]


@router.get("/messages/{message_id}/citations")
async def get_message_citations(
    message_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> MessageCitationsResponse:
    async with tenant_scope(ctx.tenant_id) as session:
        message = await session.get(MessageRow, message_id)
        if message is None:
            raise HTTPException(status_code=404, detail=f"no message {message_id}")
        citations = list(message.citations)
        bad_citation_flags = message.moderation_flags.get("bad_citation", [])
        bad_citation_ids = bad_citation_flags if isinstance(bad_citation_flags, list) else []

    resolved: list[ResolvedCitationResponse] = []
    for citation in citations:
        source_id = uuid.UUID(str(citation["source_id"]))
        entry_key = str(citation["entry_key"])
        version_id = uuid.UUID(str(citation["version_id"])) if citation.get("version_id") else None
        entry = await resolve_citation(ctx.tenant_id, source_id, entry_key, version_id)
        if entry is None:
            continue  # the entry itself was since deleted -- skip rather than 500
        resolved.append(
            ResolvedCitationResponse(
                citation_id=str(citation["citation_id"]),
                entry_key=entry_key,
                source_id=source_id,
                version_id=version_id,
                title=entry.title,
                body_md=entry.body_md,
            )
        )
    return MessageCitationsResponse(
        valid=resolved, bad_citation_ids=[str(c) for c in bad_citation_ids]
    )
