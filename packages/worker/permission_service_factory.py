"""Worker-side composition root for ``PermissionService`` -- mirrors
``api.permission_service_factory`` (delegation's FSM drive runs the same permission check
worker-side)."""

from __future__ import annotations

from adapters.permission.role_permission import RolePermissionService
from core.ports.permission import PermissionService


def get_permission_service() -> PermissionService:
    return RolePermissionService()
