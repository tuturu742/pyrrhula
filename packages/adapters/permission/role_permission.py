"""v1 PermissionService: a ``role_permission`` table lookup. Phase 5 adds
``permission_grant(principal, action, resource_id)`` alongside this table for
fine-grained RBAC  — this class's ``check()`` signature is the call-site contract
that doesn't change."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.ports.permission import ResourceType, UnknownActionError
from core.tenancy.models import Membership, RolePermission, WorkspaceMembership
from core.tenancy.scope import tenant_scope


class RolePermissionService:
    async def check(
        self,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
        action: str,
        resource_type: ResourceType,
        resource_id: uuid.UUID,
    ) -> bool:
        async with tenant_scope(tenant_id) as session:
            # role_permission carries no RLS (it's global policy data, not tenant data) —
            # readable from any tenant-scoped session.
            action_known = await session.scalar(
                select(RolePermission.id)
                .where(
                    RolePermission.action == action,
                    RolePermission.resource_type == resource_type,
                )
                .limit(1)
            )
            if action_known is None:
                raise UnknownActionError(
                    f"unknown action {action!r} for resource_type {resource_type!r}"
                )

            role: str | None
            if resource_type == "tenant":
                role = await session.scalar(
                    select(Membership.role).where(
                        Membership.tenant_id == tenant_id,
                        Membership.principal_id == principal_id,
                    )
                )
            else:
                role = await session.scalar(
                    select(WorkspaceMembership.role).where(
                        WorkspaceMembership.workspace_id == resource_id,
                        WorkspaceMembership.principal_id == principal_id,
                    )
                )

            if role is None:
                return False

            granted = await session.scalar(
                select(RolePermission.id)
                .where(
                    RolePermission.role == role,
                    RolePermission.action == action,
                    RolePermission.resource_type == resource_type,
                )
                .limit(1)
            )
            return granted is not None
