"""A generic per-server options bag on an MCP registration.

Transport-specific knobs (which engines a SearXNG instance should query, and whatever the
next transport needs) are properties of one registered server, not of the deployment that
happens to host it -- two tenants pointing at two SearXNG instances have no reason to
share an engine list. A JSONB bag keeps that from costing a migration per knob and keeps
the generic registration free of one transport's vocabulary.

Revision ID: a5c71e9b2d63
Revises: f3b8d2e41a97
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "a5c71e9b2d63"
down_revision: str | None = "f3b8d2e41a97"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mcp_server",
        sa.Column("options", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("mcp_server", "options")
