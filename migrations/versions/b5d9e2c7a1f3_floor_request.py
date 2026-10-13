"""Raised hands for an open floor.

``floor_request``: append-only, one row per raised hand -- who asked to speak again,
in which phase, during which turn (``turn_seq``). A phase whose actors take the floor in
``order: hands`` reads these to choose the next speaker. The unique key makes a resumed
turn's second raise a no-op.

Tenant-scoped with FORCE row-level security and the NULLIF policy shape (CLAUDE.md rule
4); the app role gets SELECT and INSERT only (rule 5).

Revision ID: b5d9e2c7a1f3
Revises: a7e3c9d2b5f1
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b5d9e2c7a1f3"
down_revision: str | None = "a7e3c9d2b5f1"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_POLICY = (
    "USING (tenant_id = (NULLIF(current_setting('app.tenant_id', true), ''))::uuid) "
    "WITH CHECK (tenant_id = (NULLIF(current_setting('app.tenant_id', true), ''))::uuid)"
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE public.floor_request (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES public.tenant(id) ON DELETE CASCADE,
            session_id uuid NOT NULL REFERENCES public.session(id) ON DELETE CASCADE,
            phase_key varchar(128) NOT NULL,
            principal_id uuid NOT NULL,
            turn_seq integer NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_floor_request_turn UNIQUE (session_id, phase_key, principal_id, turn_seq)
        )
        """
    )
    op.execute("ALTER TABLE public.floor_request ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ONLY public.floor_request FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON public.floor_request {_POLICY}")
    op.execute("GRANT SELECT, INSERT ON TABLE public.floor_request TO pyrrhula_app")
    # default privileges grant UPDATE/DELETE on every new table; append-only means not
    op.execute("REVOKE UPDATE, DELETE ON TABLE public.floor_request FROM pyrrhula_app")


def downgrade() -> None:
    op.execute("DROP TABLE public.floor_request")
