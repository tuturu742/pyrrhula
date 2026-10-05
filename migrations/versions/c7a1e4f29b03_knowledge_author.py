"""Knowledge authoring as a tenant permission.

Creating a source, editing its draft entries, publishing, forking and ingesting into it
were open to any signed-in member of the tenant, a viewer included. They are now gated
on ``knowledge:author``, a tenant action held by the same three roles that may archive a
source (``knowledge:archive``): owner, admin, editor. Attaching a source to a workspace
stays a workspace matter (``manage_knowledge``).

``role_permission`` is deployment-level policy data with no tenant column, so the rows
are seeded here, the way the baseline seeds every other action. The ids are fixed so
that two installs agree on them, and the insert ignores a row that is already there.

Revision ID: c7a1e4f29b03
Revises: a0000000b458
Create Date: 2026-10-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7a1e4f29b03"
down_revision: str | None = "a0000000b458"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_ACTION = "knowledge:author"
_ROWS = (
    ("08d69453-2d05-4aaa-be5c-7cadc60b500d", "owner"),
    ("f3ed2f13-1692-4ea7-9a6f-65650f611ea7", "admin"),
    ("22f711cb-1833-4f06-8abd-9e5bcc77514c", "editor"),
)


def upgrade() -> None:
    for row_id, role in _ROWS:
        op.execute(
            sa.text(
                "INSERT INTO role_permission (id, role, action, resource_type) "
                "VALUES (CAST(:id AS uuid), :role, :action, 'tenant') "
                "ON CONFLICT ON CONSTRAINT uq_role_permission DO NOTHING"
            ).bindparams(id=row_id, role=role, action=_ACTION)
        )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM role_permission WHERE action = :action").bindparams(action=_ACTION)
    )
