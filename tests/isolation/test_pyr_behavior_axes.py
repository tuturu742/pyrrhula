"""Behaviour sliders must survive a `.pyr` round trip.

A bundle carries each persona's `behavior_profile_versions`, but those reference axes by
pack id ("rpg_v1"), and a tenant only owns those rows once the workflow providing that
pack has been selected. Importing into a tenant sitting on a different workflow therefore
wrote the personas and silently dropped every slider -- the profile write failed and the
failure was swallowed as advisory. Observed on the shipped hagnaryd-mystery sample:
4 axis definitions in the tenant, 0 behaviour profiles.
"""

from __future__ import annotations

import pathlib
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


async def test_reimporting_into_the_same_workspace_does_not_duplicate(
    db_available: None,
    hagnaryd_bundle: pathlib.Path,
) -> None:
    """Re-importing a bundle must not leave two personas sharing a key.

    It does not: a resident key is skipped, not rewritten. Pinned because the failure
    mode is invisible -- two indistinguishable personas where only one holds the secrets
    and behaviour profile, so a session picking the other gets a character who knows
    nothing. (Importing into a *different* workspace is a separate copy by design, and is
    what two sets of personas in one tenant actually means.)
    """
    from sqlalchemy import select

    from adapters.encryptor.identity import IdentityEncryptor
    from core.agents.models import Persona
    from core.plugins.service import ensure_default_synced
    from core.portability.import_ import import_bundle
    from core.tenancy.scope import tenant_scope

    bundle = hagnaryd_bundle

    await ensure_default_synced()
    tenant_id, _o, workspace_id = await seed_dev_tenant(slug=f"pyrdup-{uuid.uuid4().hex[:8]}")
    data = bundle.read_bytes()
    reports = [
        await import_bundle(
            data, tenant_id, workspace_id, bundle_ref="dup", encryptor=IdentityEncryptor()
        )
        for _ in range(2)
    ]

    async with tenant_scope(tenant_id) as session:
        keys = [
            k
            for (k,) in await session.execute(
                select(Persona.key).where(Persona.workspace_id == workspace_id)
            )
        ]
    assert len(keys) == len(set(keys)), f"duplicate persona keys after re-import: {sorted(keys)}"
    assert any("key exists" in s for s in reports[1].skipped), (
        "the second import should report the personas it left alone"
    )


async def test_a_reimport_heals_secrets_the_first_pass_was_refused(
    db_available: None,
    hagnaryd_bundle: pathlib.Path,
) -> None:
    """The live failure: a tenant made through the admin console had an owner with no
    workspace membership, so every create_secret in the import was refused authorship --
    silently, into the skip list -- and the cast played a murder mystery with nothing to
    hide (0 secrets, 0 gate decisions). Re-importing after the membership exists must
    attach the secrets to the personas already there, and doing it twice must not double
    them.
    """
    from sqlalchemy import select

    from adapters.encryptor.identity import IdentityEncryptor
    from adapters.moderation.allow_all import AllowAllModerationProvider
    from adapters.permission.role_permission import RolePermissionService
    from core.plugins.service import ensure_default_synced
    from core.portability.import_ import import_bundle
    from core.secrets.models import SecretRow
    from core.tenancy.models import WorkspaceMembership
    from core.tenancy.scope import tenant_scope

    bundle = hagnaryd_bundle

    await ensure_default_synced()
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=f"pyrheal-{uuid.uuid4().hex[:8]}"
    )
    # Reproduce the broken state: the importer holds NO workspace membership.
    async with tenant_scope(tenant_id) as session:
        for row in (await session.execute(select(WorkspaceMembership))).scalars().all():
            await session.delete(row)

    data = bundle.read_bytes()
    perms = RolePermissionService()
    first = await import_bundle(
        data,
        tenant_id,
        workspace_id,
        bundle_ref="heal",
        encryptor=IdentityEncryptor(),
        importing_principal_id=owner_id,
        permission_service=perms,
        moderation_provider=AllowAllModerationProvider(),
    )
    async with tenant_scope(tenant_id) as session:
        after_first = len((await session.execute(select(SecretRow))).scalars().all())
    assert after_first == 0, "the broken import should refuse every secret"
    assert any("secret" in x for x in first.skipped)

    # The membership arrives (the backfill / the fixed admin path), and a RE-IMPORT
    # into the same workspace heals: resident personas are mapped, secrets attach.
    async with tenant_scope(tenant_id) as session:
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=owner_id,
                role="steward",
            )
        )
    for _ in range(2):  # and a third pass must not double them
        await import_bundle(
            data,
            tenant_id,
            workspace_id,
            bundle_ref="heal",
            encryptor=IdentityEncryptor(),
            importing_principal_id=owner_id,
            permission_service=perms,
            moderation_provider=AllowAllModerationProvider(),
        )
    async with tenant_scope(tenant_id) as session:
        secrets = (await session.execute(select(SecretRow))).scalars().all()
    assert len(secrets) == 11, f"expected the case's 11 secrets, got {len(secrets)}"


async def test_import_adopts_secret_mode_from_the_bundle(
    db_available: None, hagnaryd_bundle: pathlib.Path
) -> None:
    """The bundle has always carried workspace.json and nothing ever read it. The cost
    was invisible: secret_mode stayed on the leak-proof default, held secrets never
    entered anyone's context, the gate never ran, and a whole interrogation played with
    nothing to hide -- twice, in two differently-broken runs. Adoption is additive: a
    workspace that already chose a mode keeps it.
    """
    from adapters.encryptor.identity import IdentityEncryptor
    from core.plugins.service import ensure_default_synced
    from core.portability.import_ import import_bundle
    from core.tenancy.models import Workspace
    from core.tenancy.scope import tenant_scope

    bundle = hagnaryd_bundle

    await ensure_default_synced()
    tenant_id, _o, workspace_id = await seed_dev_tenant(slug=f"pyrsm-{uuid.uuid4().hex[:8]}")
    await import_bundle(
        bundle.read_bytes(),
        tenant_id,
        workspace_id,
        bundle_ref="sm",
        encryptor=IdentityEncryptor(),
    )
    async with tenant_scope(tenant_id) as session:
        ws = await session.get(Workspace, workspace_id)
        assert ws is not None and ws.settings.get("secret_mode") == "trust"

    # A workspace that already chose is never reconfigured by an import.
    tenant2, _o2, ws2_id = await seed_dev_tenant(slug=f"pyrsm-{uuid.uuid4().hex[:8]}")
    async with tenant_scope(tenant2) as session:
        ws2 = await session.get(Workspace, ws2_id)
        ws2.settings = {"secret_mode": "gate"}
    await import_bundle(
        bundle.read_bytes(),
        tenant2,
        ws2_id,
        bundle_ref="sm",
        encryptor=IdentityEncryptor(),
    )
    async with tenant_scope(tenant2) as session:
        ws2 = await session.get(Workspace, ws2_id)
        assert ws2.settings.get("secret_mode") == "gate", "an explicit choice must survive"
