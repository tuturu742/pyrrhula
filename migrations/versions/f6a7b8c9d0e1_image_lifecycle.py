"""A tenant's images and every attempt to make one usable.

``image_definition`` is a named image a tenant owns -- the name is also the runtime key a
repo selects. ``image_build`` is one attempt to make a definition usable: built by an
external builder (a later phase) or imported from a ``.pyr`` as an exact, digest-pinned
reference. Both paths share one state machine so "verified, then smoke-tested on this
tenant's own engine, before any delegation can run in it" holds for every image the
platform promotes, wherever it came from.

Both are tenant-scoped with FORCE RLS and the NULLIF policy shape (rule 4). ``image_build``
is a mutable state machine, like ``exec_environment``; the tamper-evident history is
``audit_log``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | None = None
depends_on: str | None = None

_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} USING ({_POLICY}) WITH CHECK ({_POLICY})"
    )


def _now(name: str) -> sa.Column[object]:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def upgrade() -> None:
    op.create_table(
        "image_definition",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("dockerfile", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("origin", sa.String(16), nullable=False, server_default=sa.text("'built'")),
        sa.Column("harness_key", sa.String(63), nullable=False, server_default=sa.text("''")),
        sa.Column("current_build_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "name", name="uq_image_definition_name"),
        sa.CheckConstraint("origin IN ('built', 'imported')", name="ck_image_definition_origin"),
    )
    _rls("image_definition")

    op.create_table(
        "image_build",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "definition_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("image_definition.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("builder_key", sa.String(40), nullable=True),
        sa.Column("registry_key", sa.String(40), nullable=True),
        sa.Column("dockerfile", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("content_hash", sa.String(64), nullable=False, server_default=sa.text("''")),
        sa.Column("target_ref", sa.String(512), nullable=False, server_default=sa.text("''")),
        sa.Column("external_ref", sa.String(255), nullable=False, server_default=sa.text("''")),
        sa.Column("external_url", sa.String(1024), nullable=False, server_default=sa.text("''")),
        sa.Column("reported_digest", sa.String(71), nullable=False, server_default=sa.text("''")),
        sa.Column("digest", sa.String(71), nullable=False, server_default=sa.text("''")),
        sa.Column("pinned_ref", sa.String(512), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "harness_claim",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "baked_harness",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("smoke", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("log_tail", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("error", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("origin IN ('built', 'imported')", name="ck_image_build_origin"),
        sa.CheckConstraint(
            "status IN ('queued', 'submitted', 'building', 'verifying', 'smoke_testing', "
            "'ready', 'failed', 'cancelled', 'superseded', 'deleted')",
            name="ck_image_build_status",
        ),
    )
    _rls("image_build")
    # One attempt in flight per definition: a second Build click while the first runs is
    # refused by the database, not by a read-then-write race in the service.
    op.create_index(
        "uq_image_build_one_active",
        "image_build",
        ["tenant_id", "definition_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('queued', 'submitted', 'building', 'verifying', 'smoke_testing')"
        ),
    )
    op.create_index("ix_image_build_status", "image_build", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_table("image_build")
    op.drop_table("image_definition")
