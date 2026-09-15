"""Per-repo preview recipe.

The build half of the delegation pipeline was configurable per repo -- image, setup, test
and build commands, artifact name -- while the preview half could only ever extract a
tarball and serve it as a static site. A repo could therefore build anything and preview
almost nothing. These columns are the operator's override; a `pyrrhula-preview.json` in the
repo supplies the same fields from the code side, and both fall back to the static server,
so every existing repo behaves exactly as before.

Revision ID: c8d4f1a70b52
Revises: a5c71e9b2d63
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "c8d4f1a70b52"
down_revision: str | None = "a5c71e9b2d63"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("repo", sa.Column("preview_image", sa.String(255), nullable=True))
    op.add_column("repo", sa.Column("preview_cmd", sa.String(2000), nullable=True))
    op.add_column("repo", sa.Column("preview_port", sa.Integer(), nullable=True))
    op.add_column(
        "repo",
        sa.Column("preview_env", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("repo", "preview_env")
    op.drop_column("repo", "preview_port")
    op.drop_column("repo", "preview_cmd")
    op.drop_column("repo", "preview_image")
