"""Exec-environment registry lifecycle: upsert-on-name, warm-vs-one-shot end states,
prefix removal. Live Postgres via the shared db_available fixture."""

from __future__ import annotations

import uuid

from core.exec_envs import (
    list_environments,
    mark_removed_by_prefix,
    track_run_end,
    track_run_start,
)
from core.tenancy.seed import seed_dev_tenant


async def test_warm_run_lifecycle_and_reuse(db_available: None) -> None:
    tenant_id, _, _ = await seed_dev_tenant(slug=f"envreg-{uuid.uuid4().hex[:8]}")
    persona = uuid.uuid4()
    await track_run_start(
        tenant_id,
        "pyr-env-aaaa1111-app",
        image="node:22",
        persona_id=persona,
        label="Dev A",
    )
    # No engine declaration in the test env -> not one-shot -> warm 'idle' after the run.
    await track_run_end(tenant_id, "pyr-env-aaaa1111-app", exit_code=0)
    envs = await list_environments(tenant_id)
    assert len(envs) == 1
    assert envs[0]["status"] == "idle"
    assert envs[0]["last_exit_code"] == 0
    assert envs[0]["spawned_by_label"] == "Dev A"

    # Reuse re-stamps the actor and flips back to running; still ONE row per name.
    await track_run_start(tenant_id, "pyr-env-aaaa1111-app", image="node:22", label="Dev B")
    envs = await list_environments(tenant_id)
    assert len(envs) == 1
    assert envs[0]["status"] == "running"
    assert envs[0]["spawned_by_label"] == "Dev B"

    # A crash (no exit code) marks it failed -- failed rows leave the active list.
    await track_run_end(tenant_id, "pyr-env-aaaa1111-app", exit_code=None)
    assert await list_environments(tenant_id) == []
    finished = await list_environments(tenant_id, include_finished=True)
    assert finished[0]["status"] == "failed"


async def test_mark_removed_by_prefix_only_touches_active(db_available: None) -> None:
    tenant_id, _, _ = await seed_dev_tenant(slug=f"envrm-{uuid.uuid4().hex[:8]}")
    await track_run_start(tenant_id, "pyr-env-bbbb2222-app", label="Dev A")
    await track_run_start(tenant_id, "pyr-env-bbbb2222-lib", label="Dev B")
    await track_run_start(tenant_id, "pyr-env-cccc3333-app", label="Dev C")

    assert await mark_removed_by_prefix(tenant_id, "pyr-env-bbbb2222-") == 2
    active = await list_environments(tenant_id)
    assert [e["name"] for e in active] == ["pyr-env-cccc3333-app"]
    finished = await list_environments(tenant_id, include_finished=True)
    assert {e["status"] for e in finished} == {"removed", "running"}
