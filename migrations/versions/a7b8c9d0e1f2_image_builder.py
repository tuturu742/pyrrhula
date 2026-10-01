"""External image builders the operator has declared.

Pyrrhula never runs a tenant's RUN step on infrastructure it controls: it hands the
Dockerfile to a builder the operator chose -- a CI system, a webhook in front of their own
build farm, a Docker engine behind Portainer -- and checks what comes back. This table is
those declarations.

Deployment-level, like ``image_registry``: no ``tenant_id``, no RLS, written only through
the admin API. ``config`` is data validated per kind (URLs, repository names), never a
template that is executed. The builder's own API credential is sealed on the admin
tenant's ``provider_credential`` rows, so there is no foreign key. ``allowed_tenants`` NULL
means every organization may use it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "image_builder",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("label", sa.String(120), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("credential_ref", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "registry_key",
            sa.String(40),
            sa.ForeignKey("image_registry.key", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("allowed_tenants", postgresql.JSONB(), nullable=True),
        sa.Column("isolation_ack", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "kind IN ('webhook', 'github_actions', 'portainer')", name="ck_image_builder_kind"
        ),
    )


def downgrade() -> None:
    op.drop_table("image_builder")
