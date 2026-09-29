"""Every persona holds the workspace membership its type implies.

Permission checks key on ``workspace_membership`` -- ``entity:create`` and
``secret:create`` are workspace grants -- and until now only the onboarding shortcut
gave a persona one. A cast made in the editor or imported from a bundle could speak,
but a character-creation phase ended with every player refused the sheet it had just
rolled. ``create_persona`` grants the role now; this backfills the personas that exist.

Revision ID: b1c2d3e4f5a6
Revises: a0000000b458
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a0000000b458"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO workspace_membership (tenant_id, workspace_id, principal_id, role)
            SELECT p.tenant_id, p.workspace_id, p.principal_id,
                   CASE p.persona_type
                       WHEN 'supervisor' THEN 'facilitator'
                       WHEN 'informational' THEN 'viewer'
                       ELSE 'participant'
                   END
            FROM persona p
            WHERE NOT EXISTS (
                SELECT 1 FROM workspace_membership m
                WHERE m.workspace_id = p.workspace_id AND m.principal_id = p.principal_id
            )
            """
        )
    )


def downgrade() -> None:
    # The rows are what the product now writes on its own; nothing to take back.
    pass
