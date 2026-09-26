"""Rough cost/cache-hit dashboard endpoints: "build the rough one in
Phase 1 rather than Phase 5". Read-only aggregation over ``usage_record`` -- see
``core.audit.usage_dashboard``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from core.audit.usage_dashboard import (
    UsageSummary,
    message_usage_summary,
    session_usage_summary,
    workspace_usage_summary,
)
from core.tenancy.context import RequestContext

router = APIRouter(
    tags=["usage"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


class UsageSummaryResponse(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    estimated_cost: Decimal
    cache_hit_rate: float

    @classmethod
    def from_summary(cls, summary: UsageSummary) -> UsageSummaryResponse:
        return cls(
            prompt_tokens=summary.prompt_tokens,
            completion_tokens=summary.completion_tokens,
            cached_tokens=summary.cached_tokens,
            estimated_cost=summary.estimated_cost,
            cache_hit_rate=summary.cache_hit_rate,
        )


@router.get("/messages/{message_id}/usage")
async def get_message_usage(
    message_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> UsageSummaryResponse:
    summary = await message_usage_summary(ctx.tenant_id, message_id)
    return UsageSummaryResponse.from_summary(summary)


@router.get("/sessions/{session_id}/usage")
async def get_session_usage(
    session_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> UsageSummaryResponse:
    summary = await session_usage_summary(ctx.tenant_id, session_id)
    return UsageSummaryResponse.from_summary(summary)


@router.get("/workspaces/{workspace_id}/usage")
async def get_workspace_usage(
    workspace_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> UsageSummaryResponse:
    summary = await workspace_usage_summary(ctx.tenant_id, workspace_id)
    return UsageSummaryResponse.from_summary(summary)


class UsageLimitsBody(BaseModel):
    tenant_daily_tokens: int = 0
    per_connection_daily_tokens: int = 0
    per_persona_daily_tokens: int = 0
    per_user_daily_tokens: int = 0


class UsageLimitsResponse(UsageLimitsBody):
    tenant_used_today: int = 0


@router.get("/limits")
async def get_limits_endpoint(
    ctx: RequestContext = Depends(get_request_context),
) -> UsageLimitsResponse:
    """Daily hard caps (0 = unlimited) + how much the tenant has used today."""
    from core.usage_limits import get_limits, usage_today

    limits = await get_limits(ctx.tenant_id)
    return UsageLimitsResponse(**limits, tenant_used_today=await usage_today(ctx.tenant_id))


@router.put("/limits")
async def set_limits_endpoint(
    body: UsageLimitsBody, ctx: RequestContext = Depends(get_request_context)
) -> UsageLimitsResponse:
    from api.authz import require_tenant_permission
    from core.usage_limits import set_limits, usage_today

    await require_tenant_permission(ctx, "manage_tenant")
    try:
        limits = await set_limits(ctx.tenant_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return UsageLimitsResponse(**limits, tenant_used_today=await usage_today(ctx.tenant_id))
