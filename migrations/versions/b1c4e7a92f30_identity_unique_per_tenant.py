"""Identity uniqueness is per tenant, not global.

``identity`` carried ``UNIQUE(provider, external_id)`` -- an email could exist once in
the entire deployment. That coupled tenants which are independent in every other
respect: the same person could not own an account in two organizations, and creating a
tenant failed with "email already registered" because of a row in a different tenant
that the operator cannot see or reach.

Every read of this table is already tenant-scoped -- ``verify_local`` takes a
``tenant_id``, and RLS constrains everything else -- so the global constraint bought no
safety; it only leaked one tenant's occupancy into another's provisioning.

The downgrade can fail, and that is honest rather than lossy: once two tenants share an
email, restoring the global constraint has no correct answer. Resolve the duplicates
first if you genuinely need the old shape.

Revision ID: b1c4e7a92f30
Revises: a0000000b458
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b1c4e7a92f30"
down_revision: str | None = "a0000000b458"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_OLD = "uq_identity_provider_ext"
_NEW = "uq_identity_tenant_provider_ext"


def upgrade() -> None:
    op.drop_constraint(_OLD, "identity", type_="unique")
    op.create_unique_constraint(_NEW, "identity", ["tenant_id", "provider", "external_id"])


def downgrade() -> None:
    op.drop_constraint(_NEW, "identity", type_="unique")
    op.create_unique_constraint(_OLD, "identity", ["provider", "external_id"])
