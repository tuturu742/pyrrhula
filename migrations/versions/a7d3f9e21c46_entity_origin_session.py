"""Record which session an entity was created in.

Entities belong to a workspace, deliberately: a backlog is shared, and a standup, a
triage and a planning session all legitimately look at the same one. What was missing is
where each one came from, and without that every view of them is the same view. A session
panel listed every work item the workspace had ever accumulated -- thirty of them across
eight runs, most stuck in review because nothing could put them down -- so the six items
the session in front of you actually created were indistinguishable from the rest.

Nullable on purpose, and no backfill. An entity created outside any session (through the
API, imported from a bundle, seeded by a pack) genuinely has no origin session, and
guessing one for the rows that predate this column would invent provenance rather than
record it. They read as workspace backlog, which is what they are.

Revision ID: a7d3f9e21c46
Revises: f4c8e2b91a05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "a7d3f9e21c46"
down_revision: str | None = "f4c8e2b91a05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "entity",
        sa.Column("origin_session_id", PG_UUID(as_uuid=True), nullable=True),
    )
    # ON DELETE SET NULL, not CASCADE: deleting a session must not delete the work it
    # produced. The item outlives the conversation that filed it -- that is the whole
    # reason entities are workspace-scoped in the first place.
    op.create_foreign_key(
        "entity_origin_session_id_fkey",
        "entity",
        "session",
        ["origin_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # The session panel's only query: this session's entities, newest first.
    op.create_index(
        "ix_entity_origin_session",
        "entity",
        ["tenant_id", "origin_session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_entity_origin_session", table_name="entity")
    op.drop_constraint("entity_origin_session_id_fkey", "entity", type_="foreignkey")
    op.drop_column("entity", "origin_session_id")
