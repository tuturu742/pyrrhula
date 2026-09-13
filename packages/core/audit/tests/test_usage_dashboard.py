"""C1.4 acceptance criteria for the rough cost/cache-hit dashboard aggregation."""

from __future__ import annotations

import uuid
from decimal import Decimal

from core.agents.seed import seed_dev_agent
from core.audit.models import UsageRecordRow
from core.audit.usage_dashboard import session_usage_summary, workspace_usage_summary
from core.process.skeleton import create_session
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    return tenant_id, workspace_id, sess.id


async def _add_usage_row(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    estimated_cost: str,
) -> None:
    async with tenant_scope(tenant_id) as session:
        session.add(
            UsageRecordRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                provider="echo",
                model="echo-1",
                purpose="generation",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                estimated_cost=Decimal(estimated_cost),
            )
        )


async def test_session_usage_summary_aggregates_and_computes_cache_hit_rate(
    db_available: None,
) -> None:
    tenant_id, workspace_id, session_id = await _setup("usage-session")
    await _add_usage_row(
        tenant_id,
        workspace_id,
        session_id,
        prompt_tokens=100,
        completion_tokens=20,
        cached_tokens=80,
        estimated_cost="0.01",
    )
    await _add_usage_row(
        tenant_id,
        workspace_id,
        session_id,
        prompt_tokens=50,
        completion_tokens=10,
        cached_tokens=0,
        estimated_cost="0.005",
    )

    summary = await session_usage_summary(tenant_id, session_id)

    assert summary.prompt_tokens == 150
    assert summary.completion_tokens == 30
    assert summary.cached_tokens == 80
    assert summary.estimated_cost == Decimal("0.015")
    assert summary.cache_hit_rate == 80 / 150


async def test_session_with_no_usage_records_returns_zeroed_summary(
    db_available: None,
) -> None:
    tenant_id, _workspace_id, session_id = await _setup("usage-empty")
    summary = await session_usage_summary(tenant_id, session_id)

    assert summary.prompt_tokens == 0
    assert summary.cached_tokens == 0
    assert summary.estimated_cost == Decimal(0)
    assert summary.cache_hit_rate == 0.0  # no division by zero


async def test_workspace_usage_summary_aggregates_across_sessions(db_available: None) -> None:
    tenant_id, workspace_id, session_a = await _setup("usage-workspace")
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    session_b = (await create_session(tenant_id, workspace_id, persona_id)).id

    for session_id in (session_a, session_b):
        await _add_usage_row(
            tenant_id,
            workspace_id,
            session_id,
            prompt_tokens=10,
            completion_tokens=5,
            cached_tokens=5,
            estimated_cost="0.001",
        )

    summary = await workspace_usage_summary(tenant_id, workspace_id)

    assert summary.prompt_tokens == 20
    assert summary.cached_tokens == 10
    assert summary.cache_hit_rate == 0.5
