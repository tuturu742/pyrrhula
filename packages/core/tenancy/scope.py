"""The only sanctioned way to open a database session (plan §12.1, INV-3).

Row-level security enforces tenant isolation at the database, but only if every session:

1. runs on a connection belonging to a role RLS actually applies to (not a superuser, not
   BYPASSRLS — see ``core.config.Settings.app_database_url`` vs ``database_url``);
2. sets ``app.tenant_id`` as a **transaction-local** GUC (``set_config(..., true)``)
   before running any query; and
3. does so on the *same* connection/transaction that then runs those queries.

``tenant_scope()`` is the only place all three are guaranteed at once. A transaction-
pooling connection pooler (or careless session reuse) leaking a session-level GUC to the
next tenant that borrows the connection is the single most likely way to ship a cross-
tenant leak, and it looks completely fine in review — hence ``is_local=true`` and hence
this being the one function anything is allowed to call to get a session.

``unscoped_session()`` is the narrow, explicitly-named exception: the handful of tables
that are not tenant-scoped at all — ``tenant``, ``role_permission``, and ``price_table``
(no ``tenant_id`` column, no RLS policy — pricing is the platform's knowledge of what
providers charge, not tenant data), and ``job`` (has a ``tenant_id`` column but is
deliberately not RLS-covered, since a worker must be able to claim work for any tenant;
see ``core.ports.job_queue``) — have nothing to scope. Using it for anything else is
exactly the mistake this module exists to prevent.

``admin_ddl_session()`` is a second, even narrower exception: F3.3's per-schema
generated-column/index generator (``core.entities.storage``) needs ``ALTER TABLE``/
``CREATE INDEX`` privileges the RLS-restricted ``pyrrhula_app`` role does not have and
should never be granted (a DDL grant is a far bigger blast radius than the DML grants an
app role needs, and it would let a bug anywhere reshape the schema, not just misread a
row). It connects as the migration/admin role (``database_url`` — table owner, bypasses
RLS) the same way ``migrations/env.py`` does, for schema *shape* changes only — never for
reading or writing tenant data. Every call site must be prepared to justify "why does
this need admin", the same scrutiny ``unscoped_session()``'s own exception list gets.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config import get_settings

_engine: AsyncEngine | None = None
_engine_loop: asyncio.AbstractEventLoop | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

_admin_engine: AsyncEngine | None = None
_admin_engine_loop: asyncio.AbstractEventLoop | None = None
_admin_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Rebinds to a fresh engine whenever the running event loop changes.

    In production there is exactly one loop for the process's lifetime, so this never
    fires. It matters in tests that mix direct async calls (pytest-asyncio's loop) with
    ``TestClient`` requests (its own, separate internal loop) — an asyncpg connection
    created on one loop cannot be used from another, and a naive module-level singleton
    would otherwise bind to whichever loop happened to call this first.
    """
    global _engine, _engine_loop, _sessionmaker
    current_loop = asyncio.get_running_loop()
    if _sessionmaker is None or _engine_loop is not current_loop:
        settings = get_settings()
        _engine = create_async_engine(settings.app_database_url, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
        _engine_loop = current_loop
    return _sessionmaker


@asynccontextmanager
async def tenant_scope(tenant_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """Open a session scoped to exactly one tenant for the duration of one transaction.

    Everything inside the ``async with`` block runs in a single transaction on a single
    connection, with ``app.tenant_id`` set as a transaction-local GUC — reverted the
    moment the transaction ends and the connection returns to the pool. (Postgres resets
    a transaction-local custom GUC to ``''``, not NULL, on commit/rollback; the RLS
    policies use ``NULLIF(..., '')`` so that reverted state still reads as "no tenant
    scoped" rather than raising a cast error — see the tenancy baseline migration.)
    Commits on clean
    exit, rolls back on exception (``session.begin()`` semantics).
    """
    sessionmaker = _get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        yield session


@asynccontextmanager
async def unscoped_session() -> AsyncIterator[AsyncSession]:
    """For the tables with no RLS policy to satisfy: ``tenant``, ``role_permission``,
    ``price_table`` (no ``tenant_id`` at all), and ``job`` (has one, deliberately not
    RLS-covered — see ``core.ports.job_queue``). Never use this for an RLS-covered table
    — there is no GUC set here, so RLS will show zero rows (fails safe, not silently).
    """
    sessionmaker = _get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        yield session


def _get_admin_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _admin_engine, _admin_engine_loop, _admin_sessionmaker
    current_loop = asyncio.get_running_loop()
    if _admin_sessionmaker is None or _admin_engine_loop is not current_loop:
        settings = get_settings()
        _admin_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        _admin_sessionmaker = async_sessionmaker(_admin_engine, expire_on_commit=False)
        _admin_engine_loop = current_loop
    return _admin_sessionmaker


@asynccontextmanager
async def admin_ddl_session() -> AsyncIterator[AsyncSession]:
    """Schema-DDL-only, admin-role session. See module docstring: never use this for
    tenant data reads/writes -- RLS does not apply to this connection at all."""
    sessionmaker = _get_admin_sessionmaker()
    async with sessionmaker() as session, session.begin():
        yield session


@asynccontextmanager
async def admin_registry_session() -> AsyncIterator[AsyncSession]:
    """Global-registry writes ONLY (NULL-tenant system rows: ``workflow`` templates,
    ``vocabulary_overlay`` globals, ``plugin_repository``). The app role's RLS WITH
    CHECK makes NULL-tenant rows structurally unwritable on purpose -- registering
    system content is an operator action (admin console / boot sync), never an app
    path. Reuses the admin engine; never touch tenant data through this."""
    sessionmaker = _get_admin_sessionmaker()
    async with sessionmaker() as session, session.begin():
        yield session


@asynccontextmanager
async def admin_purge_session() -> AsyncIterator[AsyncSession]:
    """Destructive-purge, admin-role session for ``core.tenancy.purge`` ONLY. The app role
    is REVOKE'd from deleting append-only tables and RLS-scoped to one tenant; truly wiping
    a tenant (or its archived rows) needs the admin role, which both bypasses RLS and holds
    the DELETE grant. Reuses the same admin engine as ``admin_ddl_session`` -- no new engine,
    so the single-session-opener rule (CLAUDE.md rule 4) still holds. Never import this from
    request-path code: it is a superuser connection with no tenant scoping whatsoever."""
    sessionmaker = _get_admin_sessionmaker()
    async with sessionmaker() as session, session.begin():
        yield session


async def dispose_engine() -> None:
    """Test/shutdown hook. Best-effort, same reasoning as
    ``api.redis_client.close_redis``: only actually disposes the engine if called from
    the loop it was created on. Disposing from a different loop can't cleanly close its
    pooled connections anyway — attempting it is what raises the cross-loop error, not a
    lack of trying."""
    global _engine, _engine_loop, _sessionmaker, _admin_engine, _admin_engine_loop
    global _admin_sessionmaker
    if _engine is not None and _engine_loop is asyncio.get_running_loop():
        await _engine.dispose()
    _engine = None
    _engine_loop = None
    _sessionmaker = None
    if _admin_engine is not None and _admin_engine_loop is asyncio.get_running_loop():
        await _admin_engine.dispose()
    _admin_engine = None
    _admin_engine_loop = None
    _admin_sessionmaker = None
