"""Votes between options.

``ballot.choice_option``: the option a voter chose in a vote between a phase's
``options`` (yes/no, a list of words) instead of between participants. NULL in a vote on
participants; both choice columns NULL is still an abstention. The table stays
append-only (CLAUDE.md rule 5): this adds a column, not a grant.

Revision ID: d8f3b2a6c9e1
Revises: b5d9e2c7a1f3
Create Date: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d8f3b2a6c9e1"
down_revision: str | None = "b5d9e2c7a1f3"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE public.ballot ADD COLUMN choice_option varchar(64)")


def downgrade() -> None:
    op.execute("ALTER TABLE public.ballot DROP COLUMN choice_option")
