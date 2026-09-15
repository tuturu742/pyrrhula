"""Record what a session is waiting for.

`advance_session` distinguishes "nothing is blocking, there is just more to do"
('active') from "your move" ('awaiting_human'), but only the caller saw that -- the
session row was left `active` either way. A session parked on a free-mode actor therefore
looked exactly like one being worked on: no error, no fault, nothing moving, and no way
to tell without reading the flow definition. Which is how one was diagnosed.

Revision ID: b9e6c2f45a18
Revises: c8d4f1a70b52
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b9e6c2f45a18"
down_revision: str | None = "c8d4f1a70b52"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("session", sa.Column("awaiting", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("session", "awaiting")
