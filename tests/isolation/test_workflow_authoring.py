"""Tenant-authored workflows: RLS isolation + read-only system templates (rule 4).

The ``workflow`` table gained a nullable ``tenant_id`` (NULL = global system template) with
the ``vocabulary_overlay`` policy shape. These are its explicit filter-omission tests: a
tenant's workflow is invisible to (and unwritable by) another tenant even with no
application-level filter, and global templates are structurally read-only through the app
role (the policy's WITH CHECK), not merely by service-layer convention.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.tenancy.scope import tenant_scope
from core.vocabulary.service import get_overlay_by_key
from core.workflows.service import (
    WorkflowNotEditableError,
    WorkflowNotFoundError,
    create_workflow,
    delete_workflow,
    get_workflow_for_tenant,
    list_workflows_for_tenant,
    set_tenant_workflow,
    update_workflow,
)

pytestmark = pytest.mark.asyncio


async def test_tenant_workflow_invisible_to_other_tenant(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    await create_workflow(tenant_a, "book-club", "Book Club")

    a_keys = {(w.key, w.tenant_id is None) for w in await list_workflows_for_tenant(tenant_a)}
    b_keys = {(w.key, w.tenant_id is None) for w in await list_workflows_for_tenant(tenant_b)}

    assert ("book-club", False) in a_keys
    assert all(key != "book-club" for key, _ in b_keys)
    # Both still see the global templates.
    assert any(is_system for _, is_system in a_keys)
    assert any(is_system for _, is_system in b_keys)

    # Even a raw, filter-omitting query from B's scope cannot see A's row.
    async with tenant_scope(tenant_b) as session:
        leaked = await session.scalar(text("SELECT count(*) FROM workflow WHERE key = 'book-club'"))
    assert leaked == 0


async def test_other_tenant_cannot_mutate_or_delete(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    await create_workflow(tenant_a, "book-club", "Book Club")

    with pytest.raises(WorkflowNotFoundError):
        await update_workflow(tenant_b, "book-club", name="Hijacked")
    with pytest.raises(WorkflowNotFoundError):
        await delete_workflow(tenant_b, "book-club")

    # A raw UPDATE from B's scope silently touches zero rows (RLS), never A's data.
    async with tenant_scope(tenant_b) as session:
        result = await session.execute(
            text("UPDATE workflow SET name = 'Hijacked' WHERE key = 'book-club'")
        )
        assert result.rowcount == 0

    row = await get_workflow_for_tenant(tenant_a, "book-club")
    assert row is not None and row.name == "Book Club"


async def test_system_templates_are_read_only(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    # Service refuses with a pointed error…
    with pytest.raises(WorkflowNotEditableError):
        await update_workflow(tenant_a, "swdev", name="Mine now")
    # …and the policy's WITH CHECK refuses even a raw write from tenant scope: the USING
    # clause lets globals be *read*, but any updated row failing the check is an error, so
    # a template cannot be modified through the app role at all.
    with pytest.raises(DBAPIError):
        async with tenant_scope(tenant_a) as session:
            await session.execute(
                text("UPDATE workflow SET name = 'Mine now' WHERE tenant_id IS NULL")
            )


async def test_tenant_row_shadows_global_on_lookup(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    await create_workflow(tenant_a, "swdev", "My Software Dev", clone_from="swdev")

    a_row = await get_workflow_for_tenant(tenant_a, "swdev")
    b_row = await get_workflow_for_tenant(tenant_b, "swdev")
    assert a_row is not None and a_row.tenant_id == tenant_a
    assert a_row.name == "My Software Dev"
    assert b_row is not None and b_row.tenant_id is None  # B still gets the template


async def test_label_overrides_materialize_as_tenant_overlay(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    await create_workflow(
        tenant_a,
        "book-club",
        "Book Club",
        overlay_key="rpg_v1",
        label_overrides={"entity.session": "Meeting", "role.facilitator": "Host"},
    )
    await set_tenant_workflow(tenant_a, "book-club")

    overlay = await get_overlay_by_key(tenant_a, "wf-book-club")
    assert overlay is not None and overlay.tenant_id == tenant_a
    assert overlay.labels["entity.session"] == "Meeting"
    assert overlay.labels["role.facilitator"] == "Host"
    # Base overlay labels survive underneath the overrides.
    # Witness a label the BASE pack actually ships: "entity.workspace" belongs to
    # the swdev overlay, not rpg_v1, so it never evidenced the merge here.
    assert overlay.labels["tab.main"] == "Character Sheet"
    # And the materialized overlay is invisible to the other tenant.
    assert await get_overlay_by_key(tenant_b, "wf-book-club") is None
