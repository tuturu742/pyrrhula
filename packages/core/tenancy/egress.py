"""Egress policy loading (S4): one place to read a tenant's
``settings["egress_policy"]`` -- ``{purpose: ["local"] | ["local","cloud"]}`` -- with
a short TTL cache so the per-turn call paths don't pay a query per model call.

The policy's ENFORCEMENT lives inside the ModelProvider port (``check_egress``); this
module only fetches the dict every ``GenerationRequest`` construction site attaches.
An absent key stays permissive by design."""

from __future__ import annotations

import time
import uuid

from core.tenancy.models import Tenant
from core.tenancy.scope import unscoped_session

_TTL_SECONDS = 30.0
_cache: dict[uuid.UUID, tuple[float, dict[str, list[str]]]] = {}


async def load_egress_policy(tenant_id: uuid.UUID) -> dict[str, list[str]]:
    now = time.monotonic()
    hit = _cache.get(tenant_id)
    if hit is not None and now - hit[0] < _TTL_SECONDS:
        return hit[1]
    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
    raw_policy = (tenant.settings or {}).get("egress_policy") if tenant else None
    policy = dict(raw_policy) if isinstance(raw_policy, dict) else {}
    _cache[tenant_id] = (now, policy)
    return policy


def invalidate_egress_policy(tenant_id: uuid.UUID) -> None:
    _cache.pop(tenant_id, None)
