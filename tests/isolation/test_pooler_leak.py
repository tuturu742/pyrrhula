"""Pooler-leak test (T0.4): borrow a connection, set the tenant GUC, return it to the
pool, borrow it again, and assert the GUC did not survive.

Uses a dedicated single-connection engine (``pool_size=1, max_overflow=0``) so the second
``engine.connect()`` is *guaranteed* to reuse the exact same physical connection as the
first — this is what actually reproduces the transaction-pooling scenario T0.2's
``tenant_scope()`` docstring warns about, rather than hoping the default pool happens to
reuse a connection.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from core.config import get_settings


async def test_tenant_guc_does_not_survive_connection_reuse(db_available: None) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.app_database_url, pool_size=1, max_overflow=0)
    tenant_id = uuid.uuid4()

    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            await conn.commit()

        # Same physical connection (pool_size=1): if `is_local=true` semantics or the
        # NULLIF handling in the RLS policies ever regress, this is what would catch a
        # tenant's ID leaking into the next logical request that reuses the connection.
        async with engine.connect() as conn2:
            leaked_value = await conn2.scalar(
                text("SELECT NULLIF(current_setting('app.tenant_id', true), '')")
            )
    finally:
        await engine.dispose()

    assert leaked_value is None
