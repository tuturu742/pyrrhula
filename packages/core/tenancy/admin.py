"""The reserved platform-admin tenant.

Platform administration is ordinary tenancy: admins are principals with owner/admin
memberships in this one reserved tenant, logging in through the normal auth flow by
naming the ``admin`` organization. The fixed UUID (mirrored in the seeding migration,
``migrations/versions/*_admin_tenant.py``) is the source of truth for "is this the
admin tenant" -- never the slug, which a colliding self-serve signup could otherwise
shadow (the migration falls back to another slug if ``admin`` is taken).
"""

from __future__ import annotations

import uuid

ADMIN_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
ADMIN_TENANT_SLUG = "admin"
