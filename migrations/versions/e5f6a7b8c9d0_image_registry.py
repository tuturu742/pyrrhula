"""Container registries the deployment's operator has declared.

Deployment-level, like ``deployment_setting`` and ``plugin_repository``: no ``tenant_id``,
no RLS, written only through the admin API. A registry is infrastructure the operator
runs or subscribes to; tenants never declare one, they use what was declared.

Each row also defines a *managed namespace* -- the path under which images built for a
tenant live -- and that is what lets the platform refuse one tenant's reference to another
tenant's image (``core.images.namespace``). The read credential lives on the admin
tenant's ``provider_credential`` rows, so there is no foreign key: those rows are
tenant-scoped behind RLS and this table is not.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d3e4f5a6b7c8"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "image_registry",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("label", sa.String(120), nullable=False, server_default=sa.text("''")),
        sa.Column("pull_host", sa.String(255), nullable=False),
        sa.Column(
            "aliases",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "path_prefix", sa.String(120), nullable=False, server_default=sa.text("'pyrrhula'")
        ),
        sa.Column(
            "path_style", sa.String(8), nullable=False, server_default=sa.text("'nested'")
        ),
        sa.Column("insecure", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("credential_ref", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "credential_username", sa.String(255), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "k8s_pull_secret", sa.String(253), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "supports_delete", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "public_by_default_ack",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
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
        sa.CheckConstraint("path_style IN ('nested', 'flat')", name="ck_image_registry_style"),
    )


def downgrade() -> None:
    op.drop_table("image_registry")
