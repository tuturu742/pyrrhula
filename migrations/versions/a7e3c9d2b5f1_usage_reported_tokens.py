"""Usage records say whose token counts they hold.

``usage_record.tokens_reported`` is true when the counts are the provider's own report
and false when they are the runtime's estimate from the visible reply, which cannot see
hidden reasoning. ``reasoning_tokens`` is the share of the completion the provider says
was reasoning, when it breaks one out. Rows written before this are estimates.

Revision ID: a7e3c9d2b5f1
Revises: f4c2a8d1e6b7
Create Date: 2026-10-11
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a7e3c9d2b5f1"
down_revision: str | None = "f4c2a8d1e6b7"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE public.usage_record ADD COLUMN reasoning_tokens integer")
    op.execute(
        "ALTER TABLE public.usage_record ADD COLUMN tokens_reported boolean NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.usage_record DROP COLUMN tokens_reported")
    op.execute("ALTER TABLE public.usage_record DROP COLUMN reasoning_tokens")
