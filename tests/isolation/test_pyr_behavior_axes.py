"""Behaviour sliders must survive a `.pyr` round trip.

A bundle carries each persona's `behavior_profile_versions`, but those reference axes by
pack id ("rpg_v1"), and a tenant only owns those rows once the workflow providing that
pack has been selected. Importing into a tenant sitting on a different workflow therefore
wrote the personas and silently dropped every slider -- the profile write failed and the
failure was swallowed as advisory. Observed on the shipped hagnaryd-mystery sample:
4 axis definitions in the tenant, 0 behaviour profiles.
"""

from __future__ import annotations

import uuid

import pytest

from core.portability.import_ import ensure_workflow_for_bundle
from core.tenancy.seed import seed_dev_tenant
from core.workflows.service import get_tenant_workflow_key, set_tenant_workflow

pytestmark = pytest.mark.asyncio


def _agent_with(pack_id: str) -> dict:
    return {"behavior_profile_versions": [{"pack_id": pack_id, "version": 1, "axis_values": {}}]}


async def test_an_explicit_workflow_key_is_pinned(db_available: None) -> None:
    tenant_id, _o, _w = await seed_dev_tenant(slug=f"pyrwf-{uuid.uuid4().hex[:8]}")

    applied = await ensure_workflow_for_bundle(tenant_id, {"workflow_key": "rpg"}, [])

    assert applied == "rpg"
    assert await get_tenant_workflow_key(tenant_id) == "rpg"


async def test_an_older_bundle_recovers_the_workflow_from_its_pack_ids(
    db_available: None,
) -> None:
    """Bundles exported before the manifest carried the key -- the shipped samples --
    still have to import with their sliders intact."""
    tenant_id, _o, _w = await seed_dev_tenant(slug=f"pyrwf-{uuid.uuid4().hex[:8]}")

    applied = await ensure_workflow_for_bundle(tenant_id, {}, [_agent_with("rpg_v1")])

    assert applied == "rpg"
    assert await get_tenant_workflow_key(tenant_id) == "rpg"


async def test_two_different_packs_are_not_guessed_at(db_available: None) -> None:
    """No single right answer, so nothing is pinned -- a guess that cannot be verified is
    worse than leaving the tenant where the operator put it."""
    tenant_id, _o, _w = await seed_dev_tenant(slug=f"pyrwf-{uuid.uuid4().hex[:8]}")
    await set_tenant_workflow(tenant_id, "default")

    applied = await ensure_workflow_for_bundle(
        tenant_id, {}, [_agent_with("rpg_v1"), _agent_with("swdev_v1")]
    )

    assert applied == ""
    assert await get_tenant_workflow_key(tenant_id) == "default"


async def test_a_workflow_this_deployment_lacks_is_not_pinned(db_available: None) -> None:
    """The plugin repository may be unreachable here (it is private in the reference
    deployment), so the workflow the bundle wants may simply not exist."""
    tenant_id, _o, _w = await seed_dev_tenant(slug=f"pyrwf-{uuid.uuid4().hex[:8]}")
    await set_tenant_workflow(tenant_id, "default")

    applied = await ensure_workflow_for_bundle(tenant_id, {"workflow_key": "no-such-wf"}, [])

    assert applied == ""
    assert await get_tenant_workflow_key(tenant_id) == "default"


async def test_pinning_the_workflow_materializes_its_axes(db_available: None) -> None:
    """The point of pinning: the pack's axis definitions exist afterwards, which is what
    a behaviour profile needs to reference."""
    from sqlalchemy import select

    from core.behavior.models import AxisDefinitionRow
    from core.plugins.service import ensure_default_synced
    from core.tenancy.scope import tenant_scope

    # A real deployment registers its plugin repositories at boot; without them there is
    # no pack directory to load axes from.
    await ensure_default_synced()
    tenant_id, _o, _w = await seed_dev_tenant(slug=f"pyrwf-{uuid.uuid4().hex[:8]}")
    await ensure_workflow_for_bundle(tenant_id, {"workflow_key": "rpg"}, [])

    async with tenant_scope(tenant_id) as session:
        packs = {
            r.pack_id for r in (await session.execute(select(AxisDefinitionRow))).scalars().all()
        }
    assert "rpg_v1" in packs, f"rpg axes were not materialized; got {packs}"
