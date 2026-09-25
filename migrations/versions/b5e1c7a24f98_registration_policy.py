"""Registration policy per tenant, and a queue for requests awaiting a decision.

`POST /auth/register` created an account in whatever organization the caller named in
the `X-Pyrrhula-Tenant` header, with no gate but the per-IP rate limiter -- so anyone who
could reach the API could obtain a viewer membership, and a session token, inside any
existing tenant. Its sibling `/auth/signup` has always checked `allow_tenant_signup`;
this one checked nothing.

A tenant now states its own policy (`open`, `request`, `closed`) and the default is
`closed`, because the behaviour being replaced is the vulnerability. `request` needs
somewhere to keep an application until an admin rules on it, which is this table.

Revision ID: b5e1c7a24f98
Revises: a7d3f9e21c46
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "b5e1c7a24f98"
down_revision: str | None = "a7d3f9e21c46"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_POLICY = "(tenant_id = (NULLIF(current_setting('app.tenant_id', true), ''))::uuid)"


def upgrade() -> None:
    op.create_table(
        "registration_request",
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
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        # Argon2, hashed by the same provider that hashes a real identity's. Held so an
        # approval can mint the account without a second round-trip to the applicant;
        # a rejected or expired row is deleted rather than kept.
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_principal_id", PG_UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_registration_request_status",
        ),
    )
    # One pending application per address per tenant: re-applying must not let someone
    # fill the queue, and an admin ruling on a name should not find two of it.
    op.execute(
        "CREATE UNIQUE INDEX uq_registration_request_pending "
        "ON public.registration_request (tenant_id, lower(email)) "
        "WHERE status = 'pending'"
    )
    op.create_index(
        "ix_registration_request_tenant_status",
        "registration_request",
        ["tenant_id", "status"],
    )
    op.execute("ALTER TABLE public.registration_request ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.registration_request FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON public.registration_request "
        f"USING {_POLICY} WITH CHECK {_POLICY}"
    )
    # Not append-only: a decision amends the row it rules on, and a rejected application
    # is removed rather than kept forever.
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.registration_request TO pyrrhula_app"
    )


def downgrade() -> None:
    op.execute("REVOKE ALL ON TABLE public.registration_request FROM pyrrhula_app")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON public.registration_request")
    op.drop_index("ix_registration_request_tenant_status", table_name="registration_request")
    op.execute("DROP INDEX IF EXISTS uq_registration_request_pending")
    op.drop_table("registration_request")
