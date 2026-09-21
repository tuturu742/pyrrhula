"""Record which branch a repository's work targets.

``GitStore`` assumed ``main`` in fourteen places, and ``clone_from`` renamed whatever
the remote called its default branch to match. That made the store internally
consistent and lost the one fact everything outbound needs: two of three repositories
on a live deployment used ``master``, and refresh failed on both with "couldn't find
remote ref main" while the third worked.

It also pinned delegated work to ``main`` for ever. An approved pull request merges into
``main`` and nowhere else, so a project that cuts a release-candidate branch and wants
agents working against *it* could not -- the target was a constant.

Existing rows get ``main``, which is what is actually checked out in their stores
(``clone_from`` normalised it), so this is accurate rather than merely a default.

Revision ID: f4c8e2b91a05
Revises: e2a7c5b18f64
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4c8e2b91a05"
down_revision: str | None = "e2a7c5b18f64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "repo",
        sa.Column(
            "default_branch",
            sa.String(255),
            nullable=False,
            server_default="main",
        ),
    )


def downgrade() -> None:
    op.drop_column("repo", "default_branch")
