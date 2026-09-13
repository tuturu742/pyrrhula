"""Idempotent bootstrap of the first platform-admin account.

When ``PYRRHULA_ADMIN_EMAIL`` + ``PYRRHULA_ADMIN_PASSWORD`` are set, api startup
ensures an owner account with that email exists in the reserved admin tenant, so a
fresh install can log in with organization ``admin`` without first using the legacy
ops token. Never updates an existing account (rotating the env password does not
rotate the stored hash -- that's the admin console's job).
"""

from __future__ import annotations

import structlog
from sqlalchemy import select

from adapters.identity.local.argon2_provider import LocalArgon2IdentityProvider
from core.config import get_settings
from core.tenancy.admin import ADMIN_TENANT_ID
from core.tenancy.models import Identity
from core.tenancy.provisioning import create_tenant_user, delete_principal
from core.tenancy.scope import tenant_scope

log = structlog.get_logger()


async def ensure_admin_account() -> None:
    settings = get_settings()
    if not (settings.admin_email and settings.admin_password):
        return
    async with tenant_scope(ADMIN_TENANT_ID) as session:
        existing = await session.scalar(
            select(Identity.id).where(
                Identity.provider == "local", Identity.external_id == settings.admin_email
            )
        )
    if existing is not None:
        return
    principal_id = await create_tenant_user(ADMIN_TENANT_ID, "Platform Admin", "owner")
    try:
        await LocalArgon2IdentityProvider().register_local(
            ADMIN_TENANT_ID, principal_id, settings.admin_email, settings.admin_password
        )
    except Exception as exc:  # noqa: BLE001 -- e.g. the email already used by another tenant
        await delete_principal(ADMIN_TENANT_ID, principal_id)
        log.warning("admin_bootstrap.identity_conflict", error=str(exc)[:200])
        return
    log.info("admin_bootstrap.account_created", email=settings.admin_email)
