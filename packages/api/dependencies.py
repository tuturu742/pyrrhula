"""Shared FastAPI dependencies for protected routes. ``get_db_session`` is the
one way a route handler gets a session — always ``tenant_scope(ctx.tenant_id)``, never a
raw session, so every future route inherits RLS enforcement without having to remember
to call ``tenant_scope()`` itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.middleware.auth import get_request_context
from core.tenancy.context import RequestContext
from core.tenancy.scope import tenant_scope


async def get_db_session(
    ctx: RequestContext = Depends(get_request_context),
) -> AsyncIterator[AsyncSession]:
    async with tenant_scope(ctx.tenant_id) as session:
        yield session
