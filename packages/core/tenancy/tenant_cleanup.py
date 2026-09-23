"""Delete the tenants a test run created, and only those.

Nearly every suite here seeds its own tenant and nothing ever removed one, so a shared
development database accumulates them run after run. The one this was written against
had reached 53,000 tenants and 93,000 principals, at which point an ordinary
``count(*)`` inside a test took minutes and the whole suite looked hung -- a CI-blocking
gate that nobody could run to the end is a gate that stops being consulted.

**Never "delete every tenant".** The same environment variables point at a real
deployment often enough that a fixture allowed to do that would eventually do it. This
takes a snapshot of what exists before the run, another after, and deletes the
difference; residue from a crashed run is the only thing it leaves behind, and the next
clean run does not grow on top of it.

Hard deletion needs the admin role -- the app role is RLS-scoped and REVOKE'd from
deleting append-only tables -- which is exactly what ``core.tenancy.purge`` exists for.
This uses the same session that tool does.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from core.tenancy.scope import admin_purge_session, dispose_engine, unscoped_session


async def existing_tenant_ids() -> set[str]:
    """Every non-library tenant id, or an empty set when there is no database at all
    (the same "skip rather than fail" posture the db_available fixtures take).

    Disposes the engine it opened. The engine is a module global keyed by event loop, and
    this runs on the *session-scoped* loop while every test runs on its own -- so the
    engine created here is replaced by the first test's and never disposed, holding its
    pool open for the length of the run. One leaked pool plus the run's own churn is
    enough to exhaust a default `max_connections` of 100, which surfaces as
    ``TooManyConnectionsError`` at the setup of whichever test happens to be next.
    """
    try:
        async with unscoped_session() as session:
            rows = await session.execute(text("SELECT id FROM tenant WHERE NOT is_library"))
            return {str(row[0]) for row in rows}
    except (SQLAlchemyError, OSError):
        return set()
    finally:
        await dispose_engine()


async def purge_tenants(tenant_ids: set[str]) -> int:
    """Delete these tenants. Returns how many were asked for; best-effort by design --
    a cleanup failure must not turn a green run red."""
    if not tenant_ids:
        return 0
    try:
        async with admin_purge_session() as session:
            await session.execute(
                text("DELETE FROM tenant WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": sorted(tenant_ids)},
            )
    except (SQLAlchemyError, OSError) as exc:  # pragma: no cover -- best effort
        print(f"\ntenant cleanup skipped: {exc}")
        return 0
    finally:
        await dispose_engine()
    return len(tenant_ids)
