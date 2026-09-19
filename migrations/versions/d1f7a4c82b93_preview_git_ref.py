"""A preview records the git ref it was built from.

Build artifacts were addressed as ``artifacts/<store>/<name>``, and ``artifact_name`` is a
single fixed string in the repo's config -- so every branch wrote the same blob key.
Two delegations running at once overwrote each other's artifact silently, and a preview
served whichever finished last. Artifacts are now keyed by ref as well, and a preview has
to record which ref it asked for so it can address the right one and so two branches of one
repository can be previewed side by side instead of colliding on a name.

The column is nullable with an empty default: existing previews keep pointing at the
pre-ref blob key, which the download path still falls back to.

Revision ID: d1f7a4c82b93
Revises: b9e6c2f45a18
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1f7a4c82b93"
down_revision: str | Sequence[str] | None = "b9e6c2f45a18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "preview_environment",
        sa.Column("git_ref", sa.String(length=255), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("preview_environment", "git_ref")
