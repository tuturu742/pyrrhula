"""INV-7: resolve a message's ``resolution_record_ids`` to their full
``ResolutionRecord`` rows -- the session view's resolution widget renders from this endpoint,
never from the message's own prose. Same shape as ``citations.py``'s per-message resolve
endpoint, one route file per "thing a message points at by id".
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from core.resolution.records import ResolutionRecordRow
from core.sessions.models import MessageRow
from core.tenancy.context import RequestContext
from core.tenancy.scope import tenant_scope

router = APIRouter(
    tags=["resolutions"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


class ResolutionRecordResponse(BaseModel):
    id: uuid.UUID
    tool_key: str
    expression: str
    rolls: list[int]
    modifiers: dict[str, object]
    total: int
    target: int | None
    outcome: str
    # True when this reply's own moderation_flags.contradiction names this record -- the
    # UI's correction badge; the widget itself always renders `total`/`outcome`
    # from this record regardless (INV-7), the flag is only about the narration's honesty.
    contradicted: bool


@router.get("/messages/{message_id}/resolutions")
async def get_message_resolutions(
    message_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> list[ResolutionRecordResponse]:
    async with tenant_scope(ctx.tenant_id) as session:
        message = await session.get(MessageRow, message_id)
        if message is None:
            raise HTTPException(status_code=404, detail=f"no message {message_id}")
        record_ids = [uuid.UUID(rid) for rid in message.resolution_record_ids]
        contradiction_flags = message.moderation_flags.get("contradiction", [])
        contradicted_ids = (
            set(contradiction_flags) if isinstance(contradiction_flags, list) else set()
        )

        records: list[ResolutionRecordRow] = []
        for record_id in record_ids:
            record = await session.get(ResolutionRecordRow, record_id)
            if record is not None:
                records.append(record)

    return [
        ResolutionRecordResponse(
            id=record.id,
            tool_key=record.tool_key,
            expression=record.expression,
            rolls=list(record.rolls),
            modifiers=record.modifiers,
            total=record.total,
            target=record.target,
            outcome=record.outcome,
            contradicted=str(record.id) in contradicted_ids,
        )
        for record in records
    ]
