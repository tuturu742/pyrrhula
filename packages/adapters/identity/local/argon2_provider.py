"""v1 IdentityProvider: local email+password auth, Argon2id hashed. OIDC/SAML are
siblings under packages/adapters/identity/ implementing the same port."""

from __future__ import annotations

import uuid

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select

from core.tenancy.models import Identity
from core.tenancy.scope import tenant_scope

_hasher = PasswordHasher()


class LocalArgon2IdentityProvider:
    def hash_password(self, password: str) -> str:
        """Hash without creating anything.

        A registration held for an admin's decision has to keep the applicant's
        credential somewhere, and it must be the same hash a real identity would carry --
        approving is then binding an existing hash to a new principal, not asking the
        person to choose a password twice.
        """
        return _hasher.hash(password)

    async def register_local_hashed(
        self, tenant_id: uuid.UUID, principal_id: uuid.UUID, email: str, password_hash: str
    ) -> Identity:
        """Create the identity from a hash produced earlier by ``hash_password``."""
        async with tenant_scope(tenant_id) as session:
            identity = Identity(
                tenant_id=tenant_id,
                principal_id=principal_id,
                provider="local",
                external_id=email,
                email=email,
                password_hash=password_hash,
            )
            session.add(identity)
            await session.flush()
            return identity

    async def register_local(
        self, tenant_id: uuid.UUID, principal_id: uuid.UUID, email: str, password: str
    ) -> Identity:
        password_hash = self.hash_password(password)
        async with tenant_scope(tenant_id) as session:
            identity = Identity(
                tenant_id=tenant_id,
                principal_id=principal_id,
                provider="local",
                external_id=email,
                email=email,
                password_hash=password_hash,
            )
            session.add(identity)
            await session.flush()
            return identity

    async def verify_local(
        self, tenant_id: uuid.UUID, email: str, password: str
    ) -> Identity | None:
        async with tenant_scope(tenant_id) as session:
            identity = await session.scalar(
                select(Identity).where(Identity.provider == "local", Identity.external_id == email)
            )
        if identity is None or identity.password_hash is None:
            return None
        try:
            _hasher.verify(identity.password_hash, password)
        except VerifyMismatchError:
            return None
        return identity
