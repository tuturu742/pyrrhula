"""Who is a platform admin, in one place.

Two deployments, two answers, and the difference is not a special case so much as what
"platform admin" *means* in each:

* **Multi-tenant.** The platform admin is a distinct person from any tenant's owner --
  the SRE who runs the box for organizations they are not a member of. They live in the
  reserved admin tenant, bootstrapped from configuration, and being an owner of some
  tenant grants nothing here.

* **Single-tenant.** There is one organization and one person running it. "Tenant owner"
  and "platform admin" are the same role by definition, so the sole organization's owner
  gets both. Without this a solo operator has *two* accounts on their own machine and
  has to sign out of their own workspace to choose an embedding model, which is not a
  security boundary -- it is a chore with no one on the other side of it.

Principals are tenant-scoped (one row per tenant), and identities are unique per tenant,
so the alternative -- writing a second principal + identity into the admin tenant at
signup -- would mean two password hashes for one human, drifting apart the first time
either is rotated. The role is derived instead of duplicated.

**It fails closed.** The single-tenant grant is scoped to the deployment's *sole*
organization: sign up a second one and `_sole_tenant()` returns None, the grant
evaporates, and the deployment is back to requiring a real admin-tenant account. That is
the correct direction -- a box with two organizations is not a box where one org's owner
should administer the other.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.config import get_settings
from core.tenancy.admin import ADMIN_TENANT_ID
from core.tenancy.models import Membership
from core.tenancy.scope import tenant_scope

_ADMIN_ROLES = frozenset({"owner", "admin"})


async def _role_in(tenant_id: uuid.UUID, principal_id: uuid.UUID) -> str | None:
    async with tenant_scope(tenant_id) as session:
        return await session.scalar(
            select(Membership.role).where(
                Membership.tenant_id == tenant_id,
                Membership.principal_id == principal_id,
            )
        )


async def is_platform_admin(tenant_id: uuid.UUID, principal_id: uuid.UUID) -> bool:
    """Whether this principal may administer the deployment itself."""
    if tenant_id == ADMIN_TENANT_ID:
        return await _role_in(tenant_id, principal_id) in _ADMIN_ROLES

    if not get_settings().single_tenant_ui:
        return False

    # Imported here rather than at module scope: middleware.tenant imports nothing from
    # this module, and keeping it that way is cheaper than reasoning about the cycle.
    from api.middleware.tenant import _sole_tenant

    sole = await _sole_tenant()
    if sole is None or sole.id != tenant_id:
        return False
    return await _role_in(tenant_id, principal_id) in _ADMIN_ROLES
