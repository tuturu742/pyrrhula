"""B1.1: process definition authoring against a live Postgres -- create/publish never
persists an invalid definition, versions increment correctly within (tenant, workspace,
key), and tenant-template (workspace_id NULL) rows version independently per tenant.
"""

from __future__ import annotations

import uuid

import pytest

from core.process.authoring import (
    DefinitionValidationError,
    create_definition,
    get_definition,
    list_definitions,
)
from core.process.dsl.fixtures import MINIMAL_MVP_FLOW
from core.tenancy.seed import seed_dev_tenant


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    return tenant_id, owner_id, workspace_id


async def test_create_definition_persists_a_valid_document(db_available: None) -> None:
    tenant_id, owner_id, workspace_id = await _setup("procdef-create")

    row = await create_definition(
        tenant_id,
        "mvp",
        "Minimal MVP Flow",
        MINIMAL_MVP_FLOW,
        workspace_id=workspace_id,
        created_by=owner_id,
    )

    assert row.version == 1
    assert row.validated_at is not None
    assert row.validation_errors == []
    assert row.definition["initial_phase"] == "arbiter_narrate"


async def test_create_definition_with_invalid_document_raises_and_persists_nothing(
    db_available: None,
) -> None:
    tenant_id, _owner_id, workspace_id = await _setup("procdef-invalid")
    broken = {**MINIMAL_MVP_FLOW, "initial_phase": "does_not_exist"}

    with pytest.raises(DefinitionValidationError) as exc_info:
        await create_definition(tenant_id, "broken", "Broken", broken, workspace_id=workspace_id)

    assert any(i.field_path == "initial_phase" for i in exc_info.value.issues)
    assert await list_definitions(tenant_id, workspace_id=workspace_id) == []


async def test_second_publish_of_the_same_key_increments_version(db_available: None) -> None:
    tenant_id, _owner_id, workspace_id = await _setup("procdef-version")

    first = await create_definition(
        tenant_id, "mvp", "V1", MINIMAL_MVP_FLOW, workspace_id=workspace_id
    )
    second = await create_definition(
        tenant_id, "mvp", "V2", MINIMAL_MVP_FLOW, workspace_id=workspace_id
    )

    assert first.version == 1
    assert second.version == 2
    assert first.id != second.id


async def test_different_keys_version_independently(db_available: None) -> None:
    tenant_id, _owner_id, workspace_id = await _setup("procdef-keys")

    a = await create_definition(
        tenant_id, "flow-a", "A", MINIMAL_MVP_FLOW, workspace_id=workspace_id
    )
    b = await create_definition(
        tenant_id, "flow-b", "B", MINIMAL_MVP_FLOW, workspace_id=workspace_id
    )

    assert a.version == 1
    assert b.version == 1


async def test_tenant_template_and_workspace_scoped_definitions_version_independently(
    db_available: None,
) -> None:
    """workspace_id NULL (tenant template) and workspace_id set (workspace-scoped) share
    a key but must not share a version counter -- the two partial unique indexes exist
    precisely so this doesn't collide."""
    tenant_id, _owner_id, workspace_id = await _setup("procdef-template")

    template = await create_definition(tenant_id, "shared-key", "Template", MINIMAL_MVP_FLOW)
    scoped = await create_definition(
        tenant_id, "shared-key", "Scoped", MINIMAL_MVP_FLOW, workspace_id=workspace_id
    )

    assert template.version == 1
    assert template.workspace_id is None
    assert scoped.version == 1
    assert scoped.workspace_id == workspace_id


async def test_get_definition_returns_the_persisted_row(db_available: None) -> None:
    tenant_id, _owner_id, workspace_id = await _setup("procdef-get")
    created = await create_definition(
        tenant_id, "mvp", "MVP", MINIMAL_MVP_FLOW, workspace_id=workspace_id
    )

    fetched = await get_definition(tenant_id, created.id)

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.definition == created.definition


async def test_get_definition_returns_none_for_unknown_id(db_available: None) -> None:
    tenant_id, _owner_id, _workspace_id = await _setup("procdef-missing")
    assert await get_definition(tenant_id, uuid.uuid4()) is None


async def test_list_definitions_filters_by_workspace(db_available: None) -> None:
    tenant_id, _owner_id, workspace_id = await _setup("procdef-list")
    await create_definition(tenant_id, "template-flow", "T", MINIMAL_MVP_FLOW)
    await create_definition(
        tenant_id, "scoped-flow", "S", MINIMAL_MVP_FLOW, workspace_id=workspace_id
    )

    scoped_only = await list_definitions(tenant_id, workspace_id=workspace_id)
    all_for_tenant = await list_definitions(tenant_id)

    assert [r.key for r in scoped_only] == ["scoped-flow"]
    assert {r.key for r in all_for_tenant} == {"template-flow", "scoped-flow"}
