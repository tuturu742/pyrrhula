"""TenantRouter port (D11, §14.3). v1 returns one DSN for every tenant; reads
``tenant.region``/``tenant.isolation_mode`` without acting on them, so the fields exist
and are exercised now — data residency (H5.5) and isolation escalation (H5.6) become
routing-table lookups behind this port, not a schema change."""

from __future__ import annotations

from typing import Protocol

from core.tenancy.models import Tenant


class TenantRouter(Protocol):
    def dsn_for(self, tenant: Tenant) -> str: ...
