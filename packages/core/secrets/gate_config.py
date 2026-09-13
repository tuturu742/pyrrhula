"""Which model runs a tenant's disclosure gate.

The gate used to be pointed at a model by ``PYRRHULA_GATE_MODEL``, a deployment-wide
environment variable. That is the wrong owner for this decision twice over: a deployment
hosts many tenants and they do not share a model choice, and the person who knows which
model to use is the tenant's admin, not whoever restarts the process.

So the choice is a tenant setting naming one of the tenant's **own model connections**.
Naming a connection rather than a provider/model string matters: the credential, api_base
and params already live on that row, managed in the same place as every other connection,
so the gate cannot become a second, invisible copy of credential handling.

What the gate needs from a model is schema discipline, not weight class -- it is a strict
JSON classifier over gists. A small, fast, structured-output-capable model is the right
choice, and pointing it away from the acting persona's own profile also stops the gate
queueing behind a large resident model on a single-GPU box (measured there: a ~1s
judgement costing minutes).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.tenancy.models import Tenant
from core.tenancy.scope import tenant_scope

_SETTING_KEY = "gate_connection_id"


async def get_gate_connection_id(tenant_id: uuid.UUID) -> uuid.UUID | None:
    """The connection this tenant runs its gate on, or None to use the acting persona's."""
    async with tenant_scope(tenant_id) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        raw = (tenant.settings or {}).get(_SETTING_KEY) if tenant else None
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        # A stale or hand-edited value must not break every secret-holding turn; the gate
        # falls back to the persona's own connection, which always exists.
        return None


async def set_gate_connection_id(
    tenant_id: uuid.UUID, connection_id: uuid.UUID | None
) -> uuid.UUID | None:
    """Point the gate at one of this tenant's connections, or clear it (None).

    Validates that the connection belongs to this tenant and is not archived, so the
    setting cannot be made to reference something the tenant cannot use -- the failure
    would otherwise surface much later, as a gate that silently conceals everything.
    """
    from core.agents.models import Agent

    async with tenant_scope(tenant_id) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        if tenant is None:
            raise ValueError("no such tenant")
        if connection_id is not None:
            agent = await session.scalar(
                select(Agent).where(
                    Agent.id == connection_id,
                    Agent.tenant_id == tenant_id,
                    Agent.archived_at.is_(None),
                )
            )
            if agent is None:
                raise ValueError("no such model connection in this tenant")
        settings = {**(tenant.settings or {})}
        if connection_id is None:
            settings.pop(_SETTING_KEY, None)
        else:
            settings[_SETTING_KEY] = str(connection_id)
        tenant.settings = settings
    return connection_id
