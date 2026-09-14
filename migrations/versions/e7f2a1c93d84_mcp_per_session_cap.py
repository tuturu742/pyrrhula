"""Per-session cap on external MCP tool calls.

An external MCP server cannot budget per session: the platform sends it only the
model's arguments, never a trusted session id (deliberately -- external servers are
untrusted). So a sample lab shipping a two-request budget enforces one shared pool
across every session hitting that process. The platform, which *does* know the session,
is the only honest place for the cap.

``mcp_server.max_calls_per_session`` is that cap (NULL = unlimited, the default:
existing registrations are unchanged). ``mcp_call_record`` is the append-only ledger it
counts -- one row per external call, effectful or not. Non-effectful calls previously
left no durable trace at all, which also made tool use invisible in a session's record.

Revision ID: e7f2a1c93d84
Revises: d4a91c2e07b5
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "e7f2a1c93d84"
down_revision: str | None = "d4a91c2e07b5"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_POLICY = "(tenant_id = (NULLIF(current_setting('app.tenant_id', true), ''))::uuid)"


def upgrade() -> None:
    op.add_column(
        "mcp_server",
        sa.Column("max_calls_per_session", sa.Integer(), nullable=True),
    )

    op.create_table(
        "mcp_call_record",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("server_key", sa.String(63), nullable=False),
        sa.Column("tool_name", sa.String(255), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("effectful", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_mcp_call_record_session_server",
        "mcp_call_record",
        ["session_id", "server_key"],
    )
    op.execute("ALTER TABLE public.mcp_call_record ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.mcp_call_record FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON public.mcp_call_record "
        f"USING {_POLICY} WITH CHECK {_POLICY}"
    )
    # Append-only (CLAUDE.md rule 5): the app role may write and read its own rows and
    # never amend them. The REVOKE is the load-bearing half -- a default-privileges rule
    # hands UPDATE/DELETE to pyrrhula_app on every new table, so granting SELECT/INSERT
    # alone leaves the table amendable (tests/isolation/test_append_only_grants.py).
    op.execute("GRANT SELECT, INSERT ON TABLE public.mcp_call_record TO pyrrhula_app")
    op.execute("REVOKE UPDATE, DELETE ON TABLE public.mcp_call_record FROM pyrrhula_app")


def downgrade() -> None:
    op.execute("REVOKE ALL ON TABLE public.mcp_call_record FROM pyrrhula_app")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON public.mcp_call_record")
    op.drop_index("ix_mcp_call_record_session_server", table_name="mcp_call_record")
    op.drop_table("mcp_call_record")
    op.drop_column("mcp_server", "max_calls_per_session")
