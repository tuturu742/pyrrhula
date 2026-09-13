"""PermissionService port (D11, §14.3, §12.1). v1 implementation is a ``role_permission``
table lookup; Phase 5 adds ``permission_grant(principal, action, resource_id)`` for
fine-grained RBAC alongside it. Call sites never change — always
``PermissionService.check(principal_id, action, resource_type, resource_id)``.
"""

from __future__ import annotations

import uuid
from typing import Literal, Protocol

ResourceType = Literal["tenant", "workspace"]


class UnknownActionError(Exception):
    """Raised when ``action`` is not a recognised action for ``resource_type`` in
    ``role_permission`` at all — a typo defence, not a permission decision."""


class PermissionService(Protocol):
    async def check(
        self,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
        action: str,
        resource_type: ResourceType,
        resource_id: uuid.UUID,
    ) -> bool:
        """``tenant_id`` comes from the caller's already-resolved ``RequestContext``, not
        from looking up ``resource_id`` — for a workspace resource, the workspace's row
        is itself behind RLS, so the tenant has to be known before it can be read. This
        also means a ``resource_id`` that doesn't belong to ``tenant_id`` simply finds no
        membership row and returns ``False``, rather than needing a separate cross-tenant
        check."""
        ...
