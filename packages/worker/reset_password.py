"""Operator password reset (no email delivery exists yet — MVP ops path):

    podman exec pyrrhula_api_1 python -m worker.reset_password <tenant-slug> <email>

Prints a freshly generated password (shown once; the user should change it after
logging in). Refuses unknown tenant/email rather than creating anything.
"""

from __future__ import annotations

import asyncio
import secrets
import sys

from argon2 import PasswordHasher
from sqlalchemy import select

from core.tenancy.models import Identity, Tenant
from core.tenancy.scope import tenant_scope, unscoped_session


async def main(slug: str, email: str) -> None:
    async with unscoped_session() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == slug))
    if tenant_id is None:
        raise SystemExit(f"no tenant with slug {slug!r}")
    async with tenant_scope(tenant_id) as session:
        identity = await session.scalar(
            select(Identity).where(
                Identity.tenant_id == tenant_id,
                Identity.provider == "local",
                Identity.external_id == email,
            )
        )
        if identity is None:
            raise SystemExit(f"no local identity for {email!r} in tenant {slug!r}")
        new_password = secrets.token_urlsafe(12)
        identity.password_hash = PasswordHasher().hash(new_password)
    print(f"password for {email} in {slug} reset to: {new_password}")
    print("(shown once -- have the user log in and change it)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
