"""Participants, ballots and message audiences.

Generic engine parts for flows where the roster plays roles (first user: Mafia between
models):

- ``session.settings``: the flow settings this session runs with (defaults merged with
  the overrides given at creation).
- ``session_persona.seat`` / ``session_persona.vars``: a participant's seat and its
  current per-participant variables (a role, a status). Current values only; every change
  is recorded in ``participant_state_change``.
- ``participant_state_change``: append-only history of participant variables -- who was
  dealt what, who was voted out, when a hidden value became public.
- ``ballot``: append-only, one row per voter per vote. Sealed until the vote's phase ends;
  the unique key makes a resumed turn's second ballot a no-op rather than a second vote.
- ``message.audience``: the principals allowed to read a message. NULL means everyone.

Both new tables are tenant-scoped with FORCE row-level security and the NULLIF policy
shape (CLAUDE.md rule 4), and the app role gets SELECT and INSERT only (rule 5).

Revision ID: e3b1f0a9c4d2
Revises: c7a1e4f29b03
Create Date: 2026-10-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e3b1f0a9c4d2"
down_revision: str | None = "c7a1e4f29b03"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_POLICY = (
    "USING (tenant_id = (NULLIF(current_setting('app.tenant_id', true), ''))::uuid) "
    "WITH CHECK (tenant_id = (NULLIF(current_setting('app.tenant_id', true), ''))::uuid)"
)


def upgrade() -> None:
    op.execute("ALTER TABLE public.session ADD COLUMN settings jsonb NOT NULL DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE public.session_persona ADD COLUMN seat smallint")
    op.execute(
        "ALTER TABLE public.session_persona ADD COLUMN vars jsonb NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute(
        "ALTER TABLE public.session_persona ADD CONSTRAINT uq_session_persona_seat "
        "UNIQUE (session_id, seat)"
    )
    op.execute("ALTER TABLE public.message ADD COLUMN audience uuid[]")

    op.execute(
        """
        CREATE TABLE public.participant_state_change (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES public.tenant(id) ON DELETE CASCADE,
            session_id uuid NOT NULL REFERENCES public.session(id) ON DELETE CASCADE,
            session_persona_id uuid NOT NULL
                REFERENCES public.session_persona(id) ON DELETE CASCADE,
            var varchar(64) NOT NULL,
            old_value jsonb,
            new_value jsonb,
            cause varchar(32) NOT NULL,
            cause_ref varchar(128),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_participant_state_change_session "
        "ON public.participant_state_change (session_id, created_at)"
    )

    op.execute(
        """
        CREATE TABLE public.ballot (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES public.tenant(id) ON DELETE CASCADE,
            session_id uuid NOT NULL REFERENCES public.session(id) ON DELETE CASCADE,
            poll_key varchar(128) NOT NULL,
            voter_session_persona_id uuid NOT NULL
                REFERENCES public.session_persona(id) ON DELETE CASCADE,
            choice_session_persona_id uuid
                REFERENCES public.session_persona(id) ON DELETE CASCADE,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_ballot_voter UNIQUE (session_id, poll_key, voter_session_persona_id)
        )
        """
    )

    for table in ("participant_state_change", "ballot"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE ONLY public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON public.{table} {_POLICY}")
        op.execute(f"GRANT SELECT, INSERT ON TABLE public.{table} TO pyrrhula_app")
        # default privileges grant UPDATE/DELETE on every new table; append-only means not
        op.execute(f"REVOKE UPDATE, DELETE ON TABLE public.{table} FROM pyrrhula_app")


def downgrade() -> None:
    op.execute("DROP TABLE public.ballot")
    op.execute("DROP TABLE public.participant_state_change")
    op.execute("ALTER TABLE public.message DROP COLUMN audience")
    op.execute("ALTER TABLE public.session_persona DROP CONSTRAINT uq_session_persona_seat")
    op.execute("ALTER TABLE public.session_persona DROP COLUMN vars")
    op.execute("ALTER TABLE public.session_persona DROP COLUMN seat")
    op.execute("ALTER TABLE public.session DROP COLUMN settings")
