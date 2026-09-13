"""Daily usage limits — the one piece of "billing" the MVP keeps: hard caps.

Limits live in ``tenant.settings["usage_limits"]`` (set by the tenant owner via
``PUT /limits``; a hosted platform would set the tenant cap from its own plan data):

    {"tenant_daily_tokens":         0,   # whole tenant, all spend
     "per_connection_daily_tokens": 0,   # per model connection (agent row)
     "per_persona_daily_tokens":    0,   # per persona
     "per_user_daily_tokens":       0}   # per human principal (directly-triggered spend)

0 / absent = unlimited. Enforcement is a **hard block**: ``ensure_within_limits``
raises ``UsageLimitExceededError`` before the model call; callers surface it (a session
pauses with the message, the assistant streams an error event, review jobs post a
note). Windows reset at midnight UTC. Aggregation reads ``usage_record`` (prompt +
completion tokens), which every metered call already writes -- limits need no second
bookkeeping. Per-user counts only spend a human directly triggered (assistant drafts/
chat); autonomous session and worker spend has no principal attribution and counts
against the other scopes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from datetime import time as dtime
from typing import Any

from sqlalchemy import func, select

from core.audit.models import UsageRecordRow
from core.tenancy.models import Tenant
from core.tenancy.scope import tenant_scope

LIMIT_KEYS = (
    "tenant_daily_tokens",
    "per_connection_daily_tokens",
    "per_persona_daily_tokens",
    "per_user_daily_tokens",
)


class UsageLimitExceededError(Exception):
    def __init__(self, scope: str, used: int, limit: int) -> None:
        self.scope, self.used, self.limit = scope, used, limit
        super().__init__(
            f"daily usage limit reached ({scope}: {used:,} of {limit:,} tokens used "
            "today; resets at midnight UTC)"
        )


def _utc_midnight() -> datetime:
    return datetime.combine(datetime.now(UTC).date(), dtime.min, tzinfo=UTC)


async def get_limits(tenant_id: uuid.UUID) -> dict[str, int]:
    async with tenant_scope(tenant_id) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        stored = (tenant.settings or {}).get("usage_limits") if tenant else None
    raw = stored if isinstance(stored, dict) else {}
    return {key: int(raw.get(key) or 0) for key in LIMIT_KEYS}


async def set_limits(tenant_id: uuid.UUID, limits: dict[str, Any]) -> dict[str, int]:
    cleaned = {}
    for key in LIMIT_KEYS:
        value = int(limits.get(key) or 0)
        if value < 0:
            raise ValueError(f"{key} must be >= 0 (0 = unlimited)")
        cleaned[key] = value
    async with tenant_scope(tenant_id) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        if tenant is None:
            raise ValueError("no such tenant")
        tenant.settings = {**(tenant.settings or {}), "usage_limits": cleaned}
    return cleaned


async def _used_since(
    tenant_id: uuid.UUID,
    since: datetime,
    *,
    agent_id: uuid.UUID | None = None,
    persona_id: uuid.UUID | None = None,
    principal_id: uuid.UUID | None = None,
) -> int:
    stmt = select(
        func.coalesce(func.sum(UsageRecordRow.prompt_tokens + UsageRecordRow.completion_tokens), 0)
    ).where(UsageRecordRow.tenant_id == tenant_id, UsageRecordRow.created_at >= since)
    if agent_id is not None:
        stmt = stmt.where(UsageRecordRow.agent_id == agent_id)
    if persona_id is not None:
        stmt = stmt.where(UsageRecordRow.persona_id == persona_id)
    if principal_id is not None:
        stmt = stmt.where(UsageRecordRow.principal_id == principal_id)
    async with tenant_scope(tenant_id) as session:
        return int(await session.scalar(stmt) or 0)


async def usage_today(tenant_id: uuid.UUID) -> int:
    return await _used_since(tenant_id, _utc_midnight())


async def ensure_within_limits(
    tenant_id: uuid.UUID,
    *,
    agent_id: uuid.UUID | None = None,
    persona_id: uuid.UUID | None = None,
    principal_id: uuid.UUID | None = None,
) -> None:
    """The pre-call gate every metered operation runs. Cheap when no limits are set
    (one settings read, zero aggregate queries)."""
    limits = await get_limits(tenant_id)
    if not any(limits.values()):
        return
    since = _utc_midnight()
    if limits["tenant_daily_tokens"]:
        used = await _used_since(tenant_id, since)
        if used >= limits["tenant_daily_tokens"]:
            raise UsageLimitExceededError("tenant", used, limits["tenant_daily_tokens"])
    if limits["per_connection_daily_tokens"] and agent_id is not None:
        used = await _used_since(tenant_id, since, agent_id=agent_id)
        if used >= limits["per_connection_daily_tokens"]:
            raise UsageLimitExceededError("connection", used, limits["per_connection_daily_tokens"])
    if limits["per_persona_daily_tokens"] and persona_id is not None:
        used = await _used_since(tenant_id, since, persona_id=persona_id)
        if used >= limits["per_persona_daily_tokens"]:
            raise UsageLimitExceededError("persona", used, limits["per_persona_daily_tokens"])
    if limits["per_user_daily_tokens"] and principal_id is not None:
        used = await _used_since(tenant_id, since, principal_id=principal_id)
        if used >= limits["per_user_daily_tokens"]:
            raise UsageLimitExceededError("user", used, limits["per_user_daily_tokens"])
