"""Which coding harness a persona works through, if any.

A key rather than a spec: what it names is resolved against ``core.harness.registry`` at
delegation time, so a tenant that withdraws a harness does not have to edit every persona
that named it.

Empty string, not NULL, and not nullable: "no harness" is the default and the overwhelming
majority, and two ways to spell it ('' and NULL) is how a query grows an ``OR`` that
someone later forgets. Existing personas keep the one-shot codegen path.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "persona",
        sa.Column("harness", sa.String(length=63), nullable=False, server_default=sa.text("''")),
    )


def downgrade() -> None:
    op.drop_column("persona", "harness")
