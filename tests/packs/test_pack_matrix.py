"""The operational definition of "generic". For every shipped pack directory
under ``packs/`` -- discovered, never hardcoded by name (a new pack is added by
adding a directory, not editing this file) -- loads the pack through the generic
``core.packs.loader``, then runs the smoke script *that pack itself ships*
(``<pack>/smoke.json``): an entity from the pack's own schema, a mutation, a
deterministic tool resolution through the unmodified ``ResolutionService``, and an FSM
transition. No pack-conditional code lives here -- every step reads from the smoke
fixture's own declared keys, generically.
"""

from __future__ import annotations

import json
import pathlib
import uuid
from typing import Any

import pytest

from adapters.permission.role_permission import RolePermissionService
from core.agents.seed import seed_dev_agent
from core.entities.mutation import mutate, transition
from core.entities.repo import get_schema
from core.entities.storage import create_entity
from core.packs.loader import load_pack
from core.process.skeleton import create_session
from core.resolution.rule_system import RuleSystemDefinition, get_rule_system
from core.resolution.service import resolve

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
# Every shipped workflow pack: the built-in default (in-tree) + the pinned default
# plugin repository's content.
_PACK_ROOTS = (_REPO_ROOT / "builtin-workflows", _REPO_ROOT / ".plugins" / "default")

_PERMISSIONS = RolePermissionService()


def _shipped_pack_dirs() -> list[pathlib.Path]:
    dirs: list[pathlib.Path] = []
    for root in _PACK_ROOTS:
        if root.is_dir():
            dirs.extend(p for p in root.iterdir() if p.is_dir())
    return sorted(dirs)


_SHIPPED_PACKS = _shipped_pack_dirs()


async def _run_smoke(
    pack_dir: pathlib.Path,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> None:
    smoke_file = pack_dir / "smoke.json"
    assert smoke_file.is_file(), f"{pack_dir.name} ships no smoke.json fixture"
    smoke: dict[str, Any] = json.loads(smoke_file.read_text())

    loaded = await load_pack(pack_dir, tenant_id, workspace_id)

    # Every declared content kind validated through the standard loaders/save paths.
    assert loaded.schema_ids, f"{pack_dir.name}: no schemas loaded"

    schema_key = smoke["schema_key"]
    schema_id = loaded.schema_ids[schema_key]
    schema_row = await get_schema(tenant_id, schema_id)
    assert schema_row is not None
    definition = schema_row.to_definition()

    # An entity from the pack's own template.
    entity = await create_entity(
        tenant_id,
        workspace_id,
        schema_id,
        definition,
        key=f"smoke-{pack_dir.name}-{uuid.uuid4().hex[:8]}",
        name=f"Smoke entity ({pack_dir.name})",
        scope_key="workspace_public",
        data=smoke["entity_data"],
    )

    # An entity mutation, through the single write path.
    mutation_changes = smoke.get("mutation_changes")
    if mutation_changes:
        await mutate(
            principal_id,
            tenant_id,
            workspace_id,
            entity.id,
            mutation_changes,
            "human",
            None,
            f"smoke-mutate-{uuid.uuid4()}",
            permission_service=_PERMISSIONS,
        )

    # A deterministic tool call, through the unmodified ResolutionService.
    resolution = smoke.get("resolution")
    if resolution:
        rule_system_row = await get_rule_system(tenant_id, resolution["rule_system_key"])
        assert rule_system_row is not None, (
            f"{pack_dir.name}: rule system {resolution['rule_system_key']!r} did not load"
        )
        rule_system = RuleSystemDefinition.from_row(rule_system_row)
        persona_id = await seed_dev_agent(tenant_id, workspace_id)
        session_row = await create_session(tenant_id, workspace_id, persona_id)
        record = await resolve(
            tenant_id=tenant_id,
            session_id=session_row.id,
            event_seq=0,
            tool_key=resolution["tool_key"],
            actor_entity_id=None,
            expression=resolution["expression"],
            check_type=resolution["check_type"],
            actor_fields=resolution["actor_fields"],
            target=resolution.get("target"),
            rule_system=rule_system,
            rule_system_id=rule_system_row.id,
            legal_check_types=None,
        )
        assert record.outcome  # a real, persisted ResolutionRecord

    # A state-machine (FSM) transition, through the single write path.
    transition_spec = smoke.get("transition")
    if transition_spec:
        pre_changes = transition_spec.get("pre_transition_changes")
        if pre_changes:
            await mutate(
                principal_id,
                tenant_id,
                workspace_id,
                entity.id,
                pre_changes,
                "human",
                None,
                f"smoke-pre-transition-{uuid.uuid4()}",
                permission_service=_PERMISSIONS,
            )
        result = await transition(
            principal_id,
            tenant_id,
            workspace_id,
            entity.id,
            transition_spec["machine_key"],
            transition_spec["trigger"],
            f"smoke-transition-{uuid.uuid4()}",
            permission_service=_PERMISSIONS,
        )
        assert result["transitioned"] is True, f"{pack_dir.name}: smoke transition did not fire"
        assert result["new_state"] == transition_spec["expected_new_state"]


@pytest.mark.parametrize("pack_dir", _SHIPPED_PACKS, ids=lambda p: p.name)
async def test_all_shipped_packs_boot_and_run_smoke_sessions(
    pack_dir: pathlib.Path, pack_tenant: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    tenant_id, workspace_id, principal_id = pack_tenant
    await _run_smoke(pack_dir, tenant_id, workspace_id, principal_id)


def test_pack_matrix_is_not_empty() -> None:
    """A guard against the parametrize silently collecting zero test cases (e.g. if
    ``packs/`` were ever misconfigured) -- pytest would otherwise report 0 passed, 0
    failed and the whole point of this suite (INV-9 CI-blocking) would go quiet.
    tightened from >=2 to >=3 now that swdev has shipped -- a future regression
    back down to two packs should fail loudly here, not slip by silently."""
    assert len(_SHIPPED_PACKS) >= 3, (
        f"expected at least the default/rpg/swdev packs, got {_SHIPPED_PACKS}"
    )
