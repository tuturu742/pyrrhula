"""Rough cost/cache-hit dashboard data: "build the rough one in Phase 1
rather than Phase 5" -- tokens + estimated spend + cache-hit rate, aggregated straight from
``usage_record``. No new table: this is a read-only query layer over data B1.7/T0.8 already
write every turn.

Cache-hit rate is ``cached_tokens / prompt_tokens`` -- the fraction of prompt tokens that
were served from a provider's prompt cache rather than freshly processed, which is exactly
what the layout work (stable prefix before volatile content) is trying to maximise.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select

from core.audit.models import UsageRecordRow
from core.tenancy.scope import tenant_scope


@dataclass(frozen=True)
class UsageSummary:
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    estimated_cost: Decimal
    cache_hit_rate: float


def _summary_from_totals(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cached_tokens: int | None,
    estimated_cost: Decimal | None,
) -> UsageSummary:
    prompt = prompt_tokens or 0
    cached = cached_tokens or 0
    return UsageSummary(
        prompt_tokens=prompt,
        completion_tokens=completion_tokens or 0,
        cached_tokens=cached,
        estimated_cost=estimated_cost or Decimal(0),
        cache_hit_rate=(cached / prompt) if prompt else 0.0,
    )


async def session_usage_summary(tenant_id: uuid.UUID, session_id: uuid.UUID) -> UsageSummary:
    async with tenant_scope(tenant_id) as session:
        prompt, completion, cached, cost = (
            await session.execute(
                select(
                    func.sum(UsageRecordRow.prompt_tokens),
                    func.sum(UsageRecordRow.completion_tokens),
                    func.sum(UsageRecordRow.cached_tokens),
                    func.sum(UsageRecordRow.estimated_cost),
                ).where(UsageRecordRow.session_id == session_id)
            )
        ).one()
    return _summary_from_totals(prompt, completion, cached, cost)


async def message_usage_summary(tenant_id: uuid.UUID, message_id: uuid.UUID) -> UsageSummary:
    """per-message token spend -- the generation call(s) that produced this one
    reply (a tool loop's intermediate calls and its final answer all share the message
    they together produced, correlated via ``UsageRecordRow.message_id``)."""
    async with tenant_scope(tenant_id) as session:
        prompt, completion, cached, cost = (
            await session.execute(
                select(
                    func.sum(UsageRecordRow.prompt_tokens),
                    func.sum(UsageRecordRow.completion_tokens),
                    func.sum(UsageRecordRow.cached_tokens),
                    func.sum(UsageRecordRow.estimated_cost),
                ).where(UsageRecordRow.message_id == message_id)
            )
        ).one()
    return _summary_from_totals(prompt, completion, cached, cost)


async def workspace_usage_summary(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> UsageSummary:
    async with tenant_scope(tenant_id) as session:
        prompt, completion, cached, cost = (
            await session.execute(
                select(
                    func.sum(UsageRecordRow.prompt_tokens),
                    func.sum(UsageRecordRow.completion_tokens),
                    func.sum(UsageRecordRow.cached_tokens),
                    func.sum(UsageRecordRow.estimated_cost),
                ).where(UsageRecordRow.workspace_id == workspace_id)
            )
        ).one()
    return _summary_from_totals(prompt, completion, cached, cost)
