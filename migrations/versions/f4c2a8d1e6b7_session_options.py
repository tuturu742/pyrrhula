"""Session options: engine behaviours a session may switch on or off.

``session.options`` holds overrides of a flow's engine-level defaults (where phase
markers go, whether phases are recapped), chosen when the session is created. Unlike
``session.settings`` -- the flow's own named settings -- these mean the same thing in
every flow, so any flow can be run with them on or off without its author declaring
anything.

Revision ID: f4c2a8d1e6b7
Revises: e3b1f0a9c4d2
Create Date: 2026-10-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f4c2a8d1e6b7"
down_revision: str | None = "e3b1f0a9c4d2"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE public.session ADD COLUMN options jsonb NOT NULL DEFAULT '{}'::jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE public.session DROP COLUMN options")
