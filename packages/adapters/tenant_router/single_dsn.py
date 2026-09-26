"""The single-DSN TenantRouter: every tenant gets the same DSN. ``tenant.region`` and
``tenant.isolation_mode`` are read (and logged) but not acted on; a router that branches
on them sits behind the same port."""

from __future__ import annotations

import structlog

from core.tenancy.models import Tenant

log = structlog.get_logger()


class SingleDsnTenantRouter:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def dsn_for(self, tenant: Tenant) -> str:
        log.debug(
            "tenant_router.route",
            tenant_id=str(tenant.id),
            region=tenant.region,
            isolation_mode=tenant.isolation_mode,
        )
        return self._dsn
