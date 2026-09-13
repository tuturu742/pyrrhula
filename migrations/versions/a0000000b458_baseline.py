"""Baseline: the whole schema as one migration.

Eighty incremental migrations were squashed into this one (seventy-eight before the
first release; then axis_definition.default_value and persona_git_credential were folded
in the same way — the repo is private with no downstream consumers, so collapsing them
into the baseline is safe and keeps the migration count at one). Nothing of them is lost
-- their reasoning lives in git history and in the model docstrings -- but nobody
installing from zero should replay months of back-and-forth to reach the present.

The schema itself is ``baseline.sql`` beside this file: a ``pg_dump`` of a database
built by the full original chain, not a hand-reassembly. That distinction is the whole
safety argument -- the squash was verified by diffing full dumps of a database built
the old way against one built by this file: identical except 19 CHECK constraints
whose array-cast expressions Postgres re-deparses equivalently on round-trip, and the
three system vocabulary overlays, deliberately removed afterwards: plugin sync owns
those (it replaces their labels from pack files on every boot, so a baked copy was
not a fallback but a second source of truth that lost).
RLS policies (with FORCE), the append-only REVOKEs,
extension creation, and the seed rows (role_permission, system vocabulary overlays, the
library and admin tenants) are all in the dump because they were all in the database.

The one thing a dump cannot carry is the login role: roles are cluster-level, so
``pyrrhula_app`` is created here first, exactly as the original tenancy baseline did --
password from ``PYRRHULA_APP_DB_PASSWORD``, with a loud dev-only fallback.

There is no downgrade below the baseline: restoring a backup IS the rollback
(deploy/k8s/README.md § Backups).

Revision ID: a0000000b458
Revises:
Create Date: 2026-09-09
"""

from __future__ import annotations

import os
import pathlib
import warnings
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.util import await_only

revision: str = "a0000000b458"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_APP_ROLE = "pyrrhula_app"
_BASELINE = pathlib.Path(__file__).with_name("baseline.sql")


def _app_role_password() -> str:
    password = os.environ.get("PYRRHULA_APP_DB_PASSWORD")
    if not password:
        password = "pyrrhula_app_dev_only"  # noqa: S105 - explicit, loud, dev-only fallback
        warnings.warn(
            "PYRRHULA_APP_DB_PASSWORD not set; creating 'pyrrhula_app' with an insecure "
            "development-only default password. Set it explicitly outside local dev.",
            stacklevel=2,
        )
    return password


def upgrade() -> None:
    bind = op.get_bind()

    # The login role first: it is cluster-level, so the dump cannot carry it, and the
    # dump's GRANT/REVOKE statements reference it.
    password = _app_role_password().replace("'", "''")
    bind.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                    CREATE ROLE {_APP_ROLE} LOGIN PASSWORD '{password}';
                END IF;
            END
            $$;
            """
        )
    )

    sql = _BASELINE.read_text()
    raw = getattr(bind.connection, "driver_connection", None)
    if raw is not None and type(raw).__module__.startswith("asyncpg"):
        # A pg_dump script is thousands of statements, some with dollar-quoted bodies;
        # splitting it ourselves would mean writing a SQL parser. asyncpg's simple-query
        # protocol runs a multi-statement string as-is, and inside Alembic's run_sync
        # greenlet the awaitable bridges back with await_only.
        await_only(raw.execute(sql))
    else:  # pragma: no cover -- sync drivers (psycopg) take multi-statement scripts directly
        bind.exec_driver_sql(sql)

    # pg_dump's preamble empties search_path for the session (a hardening default).
    # Alembic still has to stamp alembic_version on this same connection, so put it back.
    bind.execute(sa.text("SET search_path TO public"))


def downgrade() -> None:
    raise RuntimeError("there is no downgrade below the baseline; restore from a backup instead")
