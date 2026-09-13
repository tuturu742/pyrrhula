"""Daily usage limits: aggregation windows, per-scope enforcement, unlimited default.
Live Postgres via the shared db_available fixture."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from core.audit.models import UsageRecordRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant
from core.usage_limits import (
    UsageLimitExceededError,
    ensure_within_limits,
    get_limits,
    set_limits,
    usage_today,
)


async def _spend(
    tenant_id: uuid.UUID,
    tokens: int,
    *,
    agent_id: uuid.UUID | None = None,
    persona_id: uuid.UUID | None = None,
    principal_id: uuid.UUID | None = None,
    days_ago: int = 0,
) -> None:
    async with tenant_scope(tenant_id) as session:
        row = UsageRecordRow(
            tenant_id=tenant_id,
            provider="echo",
            model="echo-1",
            purpose="generation",
            prompt_tokens=tokens,
            completion_tokens=0,
            agent_id=agent_id,
            persona_id=persona_id,
            principal_id=principal_id,
        )
        session.add(row)
        await session.flush()
        if days_ago:
            row.created_at = datetime.now(UTC) - timedelta(days=days_ago)


async def test_unlimited_by_default(db_available: None) -> None:
    tenant_id, _, _ = await seed_dev_tenant(slug=f"lim-none-{uuid.uuid4().hex[:8]}")
    await _spend(tenant_id, 10_000_000)
    await ensure_within_limits(tenant_id)  # no limits set -> never raises


async def test_tenant_daily_limit_blocks_and_ignores_yesterday(db_available: None) -> None:
    tenant_id, _, _ = await seed_dev_tenant(slug=f"lim-tenant-{uuid.uuid4().hex[:8]}")
    await set_limits(tenant_id, {"tenant_daily_tokens": 1000})
    await _spend(tenant_id, 5000, days_ago=1)  # yesterday never counts
    await ensure_within_limits(tenant_id)
    await _spend(tenant_id, 1200)
    assert await usage_today(tenant_id) == 1200
    with pytest.raises(UsageLimitExceededError) as excinfo:
        await ensure_within_limits(tenant_id)
    assert excinfo.value.scope == "tenant"


async def test_scoped_limits(db_available: None) -> None:
    tenant_id, owner_id, _ = await seed_dev_tenant(slug=f"lim-scope-{uuid.uuid4().hex[:8]}")
    hot_conn, cold_conn = uuid.uuid4(), uuid.uuid4()
    persona = uuid.uuid4()
    await set_limits(
        tenant_id,
        {
            "per_connection_daily_tokens": 500,
            "per_persona_daily_tokens": 800,
            "per_user_daily_tokens": 300,
        },
    )
    await _spend(tenant_id, 600, agent_id=hot_conn)
    await _spend(tenant_id, 900, persona_id=persona)
    await _spend(tenant_id, 400, principal_id=owner_id)

    # The hot connection is over; a different connection is fine.
    with pytest.raises(UsageLimitExceededError) as exc:
        await ensure_within_limits(tenant_id, agent_id=hot_conn)
    assert exc.value.scope == "connection"
    await ensure_within_limits(tenant_id, agent_id=cold_conn)

    with pytest.raises(UsageLimitExceededError) as exc:
        await ensure_within_limits(tenant_id, persona_id=persona)
    assert exc.value.scope == "persona"

    with pytest.raises(UsageLimitExceededError) as exc:
        await ensure_within_limits(tenant_id, principal_id=owner_id)
    assert exc.value.scope == "user"
    # Autonomous spend (no principal) is never blocked by the user scope.
    await ensure_within_limits(tenant_id)


async def test_set_limits_validation(db_available: None) -> None:
    tenant_id, _, _ = await seed_dev_tenant(slug=f"lim-val-{uuid.uuid4().hex[:8]}")
    with pytest.raises(ValueError):
        await set_limits(tenant_id, {"tenant_daily_tokens": -1})
    limits = await set_limits(tenant_id, {"tenant_daily_tokens": 42})
    assert (await get_limits(tenant_id))["tenant_daily_tokens"] == 42
    assert limits["per_user_daily_tokens"] == 0
