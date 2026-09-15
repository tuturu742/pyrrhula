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


async def test_moderation_resolves_per_tenant_without_changing_the_port(
    db_available: None,
) -> None:
    """Two tenants can reasonably want different moderation, so the model is a setting.
    The port stays tenant-agnostic -- ``ModerationProvider.check()`` is unchanged; it is
    the composition root that resolves which adapter to build, which is where a selection
    decision belongs (CLAUDE.md rule 12)."""
    import inspect

    from adapters.moderation.allow_all import AllowAllModerationProvider
    from core.moderation_selection import build_provider, moderation_choice
    from core.ports.moderation import ModerationProvider

    assert "tenant" not in inspect.signature(ModerationProvider.check).parameters, (
        "the port must not have grown a tenant argument"
    )

    a_id, _a_owner, a_ws = await seed_dev_tenant(slug=f"mod-a-{uuid.uuid4().hex[:8]}")
    b_id, _b_owner, _b_ws = await seed_dev_tenant(slug=f"mod-b-{uuid.uuid4().hex[:8]}")

    # Nothing set: the deployment default, which is allow-all on a bare test deployment.
    model, _base = await moderation_choice(a_id, a_ws)
    assert isinstance(build_provider(model, None), AllowAllModerationProvider)

    # One tenant opts into a classifier; the other is untouched.
    await _set(Tenant, a_id, a_id, {"moderation_model": "openai/guard-1"})
    a_model, _ = await moderation_choice(a_id, a_ws)
    b_model, _ = await moderation_choice(b_id, None)
    assert a_model == "openai/guard-1"
    assert b_model == ""
    assert not isinstance(build_provider(a_model, None), AllowAllModerationProvider)

    # A workspace can be stricter than its tenant.
    await _set(Workspace, a_ws, a_id, {"moderation_model": "openai/guard-strict"})
    ws_model, _ = await moderation_choice(a_id, a_ws)
    assert ws_model == "openai/guard-strict"


async def test_clearing_a_workspace_override_restores_inheritance(db_available: None) -> None:
    """Blank in the UI means inherit, so the endpoint must REMOVE the key rather than
    store an empty value -- a stored 0 is a deliberate choice of zero and the resolver is
    right to honour it, which is exactly why "clear" cannot be spelled that way."""
    from api.routes.workspaces import WorkspaceSettingsBody, patch_workspace_settings
    from core.tenancy.context import RequestContext
    from core.tenancy.models import WorkspaceMembership

    tenant_id, owner_id, workspace_id = await seed_dev_tenant(slug=f"cfg-{uuid.uuid4().hex[:8]}")
    await _set(Tenant, tenant_id, tenant_id, {"max_review_rounds": 7})
    # manage_workspace rides on the membership, not on being the tenant's owner.
    async with tenant_scope(tenant_id) as session:
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=owner_id,
                role="facilitator",
            )
        )
    ctx = RequestContext(tenant_id=tenant_id, principal_id=owner_id)

    await patch_workspace_settings(workspace_id, WorkspaceSettingsBody(max_review_rounds=1), ctx)
    assert await resolved_setting(tenant_id, workspace_id, "max_review_rounds", 2) == 1

    # Explicit null clears the override and the tenant's value applies again.
    await patch_workspace_settings(workspace_id, WorkspaceSettingsBody(max_review_rounds=None), ctx)
    assert await resolved_setting(tenant_id, workspace_id, "max_review_rounds", 2) == 7
    assert await source_of(tenant_id, workspace_id, "max_review_rounds") == "tenant"
