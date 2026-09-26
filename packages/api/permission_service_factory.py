"""The one place a concrete ``PermissionService`` adapter is selected. Mirrors
``api.model_provider_factory``'s composition-root pattern for the same port (CLAUDE.md
rule 12: call sites depend on the port, never construct a specific adapter themselves).
"""

from __future__ import annotations

from adapters.permission.role_permission import RolePermissionService
from core.ports.permission import PermissionService


def get_permission_service() -> PermissionService:
    return RolePermissionService()
