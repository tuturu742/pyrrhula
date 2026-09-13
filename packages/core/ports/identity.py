"""IdentityProvider port (D11, §12.1). v1 is local password auth; OIDC/SAML (H5.1) are
adapter swaps behind the same port — nothing downstream references a human-specific
"user" table, only ``Principal`` + ``Identity``.

Tenant is resolved *before* identity verification (subdomain/header/single-tenant-default,
T0.6), so ``verify_local`` takes ``tenant_id`` explicitly rather than searching for an
email across every tenant — ``Identity`` is RLS-protected like any other tenant-scoped
table, and a wrong tenant guess simply finds no matching row, which is the correct
login-failure behaviour anyway.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from core.tenancy.models import Identity


class IdentityProvider(Protocol):
    async def register_local(
        self, tenant_id: uuid.UUID, principal_id: uuid.UUID, email: str, password: str
    ) -> Identity: ...

    async def verify_local(
        self, tenant_id: uuid.UUID, email: str, password: str
    ) -> Identity | None: ...
