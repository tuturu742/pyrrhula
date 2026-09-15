"""Per-server timeout and result cap on an MCP registration.

A timeout is a property of a server, not of a deployment: a lookup tool answers in
milliseconds while an engine tool legitimately runs a build for ten minutes. One global
number means tuning for the slowest and letting every other server hang that long when it
dies. Same argument as max_calls_per_session, and the same home -- the registration,
where the person attaching the server is already deciding things about it.

NULL means "use the platform default", so every existing registration is unchanged.

Revision ID: f3b8d2e41a97
Revises: e7f2a1c93d84
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3b8d2e41a97"
down_revision: str | None = "e7f2a1c93d84"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mcp_server", sa.Column("timeout_seconds", sa.Integer(), nullable=True))
    op.add_column("mcp_server", sa.Column("max_result_chars", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_server", "max_result_chars")
    op.drop_column("mcp_server", "timeout_seconds")
