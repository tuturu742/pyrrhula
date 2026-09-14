"""Per-persona generation overrides.

A cast sharing one model connection converges into one voice -- observed live as
suspects trading each other's sentences verbatim by round three. ``persona.params``
carries per-persona sampling overrides (temperature, seed, penalties), merged over the
connection's own params at generation time, so distinct voices need no cloned
connections.

Revision ID: d4a91c2e07b5
Revises: b1c4e7a92f30
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "d4a91c2e07b5"
down_revision: str | None = "b1c4e7a92f30"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "persona",
        sa.Column("params", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("persona", "params")
