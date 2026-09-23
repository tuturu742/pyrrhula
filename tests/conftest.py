"""Shared pytest fixtures.

Tenant/DB fixtures (two tenants + the library tenant, per-table row seeding) land with
T0.4. Kept minimal here so T0.1's CI has something real to collect.

The one thing that lives here rather than in a per-suite conftest is tenant cleanup.
``tests/isolation`` alone had left 23,558 tenants in the development database, more than
every other suite combined, because seeding two fresh tenants is the first line of
nearly every isolation test. See ``core.tenancy.tenant_cleanup``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio

from core.tenancy.tenant_cleanup import existing_tenant_ids, purge_tenants


@pytest_asyncio.fixture(scope="session", autouse=True)
async def purge_tenants_this_run_created() -> AsyncIterator[None]:
    before = await existing_tenant_ids()
    yield
    await purge_tenants(await existing_tenant_ids() - before)
