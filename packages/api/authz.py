"""Small authorization helpers for route handlers, so a permission check is one call and
never inlined role logic (CLAUDE.md rule 12: go through ``PermissionService.check``).
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException

from api.permission_service_factory import get_permission_service
from core.ports.permission import ResourceType, UnknownActionError
from core.tenancy.context import RequestContext


async def require_permission(
    ctx: RequestContext,
    action: str,
    resource_type: ResourceType,
    resource_id: uuid.UUID,
) -> None:
    """Raise 403 unless the acting principal is granted ``action`` on the resource. An
    unknown action (typo defence, not a decision) is treated as "not granted" -> 403."""
    try:
        granted = await get_permission_service().check(
            ctx.tenant_id, ctx.principal_id, action, resource_type, resource_id
        )
    except UnknownActionError:
        granted = False
    if not granted:
        raise HTTPException(status_code=403, detail=f"not permitted: {action}")


async def require_tenant_permission(ctx: RequestContext, action: str) -> None:
    """``require_permission`` for a tenant-scoped action -- the check keys on the caller's
    tenant ``Membership`` role, so the resource id is the tenant itself."""
    await require_permission(ctx, action, "tenant", ctx.tenant_id)
