"""The resolution chain, layer by layer."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import update

from core.settings.resolve import resolved_setting, resolved_settings, source_of
from core.tenancy.models import Tenant, Workspace
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

pytestmark = pytest.mark.asyncio


async def _set(table, row_id: uuid.UUID, tenant_id: uuid.UUID, settings: dict) -> None:
    async with tenant_scope(tenant_id) as session:
        await session.execute(update(table).where(table.id == row_id).values(settings=settings))


async def test_the_chain_resolves_most_specific_first(db_available: None) -> None:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"cfg-{uuid.uuid4().hex[:8]}")

    # Nothing set anywhere: the system default stands.
    assert await resolved_setting(tenant_id, workspace_id, "gate_model", "fallback") == "fallback"
    assert await source_of(tenant_id, workspace_id, "gate_model") == "default"

    # A tenant default applies to its workspaces.
    await _set(Tenant, tenant_id, tenant_id, {"gate_model": "tenant-choice"})
    assert (
        await resolved_setting(tenant_id, workspace_id, "gate_model", "fallback") == "tenant-choice"
    )
    assert await source_of(tenant_id, workspace_id, "gate_model") == "tenant"

    # A workspace overrides its tenant.
    await _set(Workspace, workspace_id, tenant_id, {"gate_model": "workspace-choice"})
    assert (
        await resolved_setting(tenant_id, workspace_id, "gate_model", "fallback")
        == "workspace-choice"
    )
    assert await source_of(tenant_id, workspace_id, "gate_model") == "workspace"

    # Asked without a workspace, the tenant layer answers -- a tenant-wide job has no
    # workspace to ask on behalf of and must not invent one.
    assert await resolved_setting(tenant_id, None, "gate_model", "fallback") == "tenant-choice"


async def test_a_stored_falsy_value_is_a_choice_not_an_absence(db_available: None) -> None:
    """Absent means inherit; present means chosen. If ``0`` fell through, a workspace
    could never deliberately turn something off, and an operator could never tell "not set
    here" from "set to nothing"."""
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"cfg-{uuid.uuid4().hex[:8]}")
    await _set(Tenant, tenant_id, tenant_id, {"max_review_rounds": 5, "reranker": True})
    await _set(Workspace, workspace_id, tenant_id, {"max_review_rounds": 0, "reranker": False})

    assert await resolved_setting(tenant_id, workspace_id, "max_review_rounds", 2) == 0
    assert await resolved_setting(tenant_id, workspace_id, "reranker", True) is False
    assert await source_of(tenant_id, workspace_id, "max_review_rounds") == "workspace"


async def test_several_keys_resolve_independently_in_one_pass(db_available: None) -> None:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"cfg-{uuid.uuid4().hex[:8]}")
    await _set(Tenant, tenant_id, tenant_id, {"gate_model": "t-gate", "assistant_model": "t-asst"})
    await _set(Workspace, workspace_id, tenant_id, {"gate_model": "w-gate"})

    effective = await resolved_settings(
        tenant_id,
        workspace_id,
        {"gate_model": "d-gate", "assistant_model": "d-asst", "moderation_model": "d-mod"},
    )
    assert effective == {
        "gate_model": "w-gate",  # workspace
        "assistant_model": "t-asst",  # tenant
        "moderation_model": "d-mod",  # default
    }


async def test_one_tenants_settings_never_reach_another(db_available: None) -> None:
    """The layers are read inside tenant_scope, so RLS applies to them like everything
    else -- a settings read is not a hole in tenancy."""
    a_id, _a_owner, a_ws = await seed_dev_tenant(slug=f"cfg-a-{uuid.uuid4().hex[:8]}")
    b_id, _b_owner, b_ws = await seed_dev_tenant(slug=f"cfg-b-{uuid.uuid4().hex[:8]}")
    await _set(Tenant, a_id, a_id, {"gate_model": "a-secret-choice"})

    assert await resolved_setting(b_id, b_ws, "gate_model", "fallback") == "fallback"
    assert await resolved_setting(a_id, a_ws, "gate_model", "fallback") == "a-secret-choice"


async def test_review_rounds_resolve_per_workspace_and_stay_under_the_ceiling(
    db_available: None,
) -> None:
    """How many times a reviewer may send work back is workflow policy, so a workspace
    sets it. The deployment keeps only the ceiling, because an unbounded review loop
    spends a tenant's API budget in a cycle nobody watched."""
    from worker.review import max_review_rounds

    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"cfg-{uuid.uuid4().hex[:8]}")

    assert await max_review_rounds(tenant_id, workspace_id) == 2  # system default

    await _set(Tenant, tenant_id, tenant_id, {"max_review_rounds": 4})
    assert await max_review_rounds(tenant_id, workspace_id) == 4

    await _set(Workspace, workspace_id, tenant_id, {"max_review_rounds": 1})
    assert await max_review_rounds(tenant_id, workspace_id) == 1

    # A workspace cannot exceed the deployment's ceiling, nor go negative.
    await _set(Workspace, workspace_id, tenant_id, {"max_review_rounds": 9999})
    assert await max_review_rounds(tenant_id, workspace_id) == 10
    await _set(Workspace, workspace_id, tenant_id, {"max_review_rounds": -3})
    assert await max_review_rounds(tenant_id, workspace_id) == 0

    # Nonsense stored by hand falls back rather than crashing a worker mid-review.
    await _set(Workspace, workspace_id, tenant_id, {"max_review_rounds": "lots"})
    assert await max_review_rounds(tenant_id, workspace_id) == 2
