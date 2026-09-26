"""TenantRouter port. v1 returns one DSN for every tenant; reads
``tenant.region``/``tenant.isolation_mode`` without acting on them, so the fields exist
and are exercised now — data residency and isolation escalation become
routing-table lookups behind this port, not a schema change."""

from __future__ import annotations

from typing import Protocol

from core.tenancy.models import Tenant


class TenantRouter(Protocol):
    def dsn_for(self, tenant: Tenant) -> str: ...
