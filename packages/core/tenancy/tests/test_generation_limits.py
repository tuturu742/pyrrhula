"""Per-tenant generation ceilings: read from tenant settings, fail closed."""

from __future__ import annotations

import uuid

from core.tenancy.generation_limits import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_SECONDS,
    SETTINGS_KEY,
    invalidate_generation_limits,
    load_generation_limits,
)
from core.tenancy.models import Tenant
from core.tenancy.scope import unscoped_session
from core.tenancy.seed import seed_dev_tenant


async def test_a_tenant_without_the_setting_gets_the_defaults(db_available: None) -> None:
    tenant_id, _owner, _ws = await seed_dev_tenant(slug=f"genlim-{uuid.uuid4().hex[:8]}")
    limits = await load_generation_limits(tenant_id)
    assert limits.max_seconds == DEFAULT_MAX_SECONDS
    assert limits.max_chars == DEFAULT_MAX_CHARS


async def test_a_tenant_can_set_its_own_ceilings(db_available: None) -> None:
    tenant_id, _owner, _ws = await seed_dev_tenant(slug=f"genlim-{uuid.uuid4().hex[:8]}")
    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        tenant.settings = {
            **(tenant.settings or {}),
            SETTINGS_KEY: {"max_seconds": 30, "max_chars": 4000},
        }
    invalidate_generation_limits(tenant_id)

    limits = await load_generation_limits(tenant_id)
    assert limits.max_seconds == 30
    assert limits.max_chars == 4000


async def test_no_ceiling_is_not_a_settable_value(db_available: None) -> None:
    """These fail CLOSED. Zero, negative or unparseable means the default, never
    'unbounded' -- unbounded is the state they exist to end, so it must not be reachable
    by typing 0 into a settings form."""
    tenant_id, _owner, _ws = await seed_dev_tenant(slug=f"genlim-{uuid.uuid4().hex[:8]}")
    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        tenant.settings = {
            **(tenant.settings or {}),
            SETTINGS_KEY: {"max_seconds": 0, "max_chars": -1},
        }
    invalidate_generation_limits(tenant_id)

    limits = await load_generation_limits(tenant_id)
    assert limits.max_seconds == DEFAULT_MAX_SECONDS
    assert limits.max_chars == DEFAULT_MAX_CHARS


async def test_a_nonsense_setting_falls_back_rather_than_raising(db_available: None) -> None:
    tenant_id, _owner, _ws = await seed_dev_tenant(slug=f"genlim-{uuid.uuid4().hex[:8]}")
    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        tenant.settings = {**(tenant.settings or {}), SETTINGS_KEY: "three hundred"}
    invalidate_generation_limits(tenant_id)

    limits = await load_generation_limits(tenant_id)
    assert limits.max_seconds == DEFAULT_MAX_SECONDS
