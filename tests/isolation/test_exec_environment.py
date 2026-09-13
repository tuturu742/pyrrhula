"""exec_environment: RLS isolation (rule 4).

The tracked exec-environment registry is a standard tenant-scoped table (RLS FORCE,
NULLIF policy shape). Filter-omission tests: another tenant cannot see or mutate a
tracked environment, even via raw SQL without a tenant filter.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from core.exec_envs import (
    get_environment,
    list_environments,
    set_status,
    track_run_end,
    track_run_start,
)
from core.tenancy.scope import tenant_scope

pytestmark = pytest.mark.asyncio


async def test_environment_invisible_to_other_tenant(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    await track_run_start(
        tenant_a,
        "pyr-env-abcd1234-proj",
        engine_key=None,
        image="node:22",
        label="Dev A",
    )

    mine = await list_environments(tenant_a)
    assert [e["name"] for e in mine] == ["pyr-env-abcd1234-proj"]
    assert mine[0]["status"] == "running"
    assert await list_environments(tenant_b) == []

    async with tenant_scope(tenant_b) as session:
        leaked = await session.scalar(
            text("SELECT count(*) FROM exec_environment WHERE name = 'pyr-env-abcd1234-proj'")
        )
    assert leaked == 0

    env_id = mine[0]["id"]
    assert await get_environment(tenant_b, env_id) is None
    assert await set_status(tenant_b, env_id, "killed") is False
    # And the run-end upsert from the wrong tenant touches nothing.
    await track_run_end(tenant_b, "pyr-env-abcd1234-proj", exit_code=0)
    assert (await list_environments(tenant_a))[0]["status"] == "running"
