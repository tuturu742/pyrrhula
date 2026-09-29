"""Whether an entry's activation keys were derived from its title or written by a person.

Derivation fills in keys only for an entry that has none, at publish. The marker is what
makes "none" expressible afterwards: clearing the keys of an entry that was derived leaves
this true, so the next publish does not simply put them back. Editing them to something
else hands ownership over and clears it.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_entry",
        sa.Column("keys_derived", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("knowledge_entry", "keys_derived")
