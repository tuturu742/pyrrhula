"""F3.1's own isolation + acceptance tests for ``entity_schema``. The generic
filter-omission coverage lives in ``test_filter_omission_matrix.py``/
``test_coverage_guard.py`` (extended in this PR); this file proves the specific claims
F3.1's acceptance criteria name: bad CEL is rejected at save with the offending
expression named, a compliant write computes derived fields and rejects constraint
violations, and the same machinery expresses two unrelated domains identically.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text

from core.entities.repo import get_schema, list_schema_versions, save_schema
from core.entities.schema import ConstraintDef, DerivedDef, EntitySchemaDefinition, FieldDef
from core.entities.validation import (
    ConstraintViolationError,
    FieldValidationError,
    SchemaValidationError,
    validate_and_prepare_write,
)
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope


async def _workspace_id(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def test_bad_cel_rejected_at_save_with_field_level_error(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)

    bad_definition = EntitySchemaDefinition(
        fields=[FieldDef(key="xp", type="integer")],
        derived=[DerivedDef(key="level", type="integer", expression="fields.nonexistent + 1")],
    )
    try:
        await save_schema(tenant_a, workspace_id, "bad-schema", 1, bad_definition)
        raise AssertionError("expected SchemaValidationError")
    except SchemaValidationError as exc:
        assert any("derived[0].expression" in issue.field_path for issue in exc.issues)
        assert any("nonexistent" in issue.message for issue in exc.issues)

    # A valid schema round-trips through the API typed.
    good_definition = EntitySchemaDefinition(
        fields=[FieldDef(key="xp", type="integer", minimum=0)],
        derived=[DerivedDef(key="level", type="integer", expression="fields.xp / 100 + 1")],
    )
    row = await save_schema(tenant_a, workspace_id, "good-schema", 1, good_definition)
    fetched = await get_schema(tenant_a, row.id)
    assert fetched is not None
    round_tripped = fetched.to_definition()
    assert round_tripped.fields[0].key == "xp"
    assert round_tripped.derived[0].expression == "fields.xp / 100 + 1"


async def test_constraint_violation_rejected_and_derived_fields_computed_on_write(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants

    definition = EntitySchemaDefinition(
        fields=[FieldDef(key="dexterity", type="integer"), FieldDef(key="xp", type="integer")],
        derived=[DerivedDef(key="level", type="integer", expression="fields.xp / 100")],
        constraints=[ConstraintDef(expression="fields.dexterity >= 13 && fields.level >= 3")],
    )

    # Compliant write: derived is computed, constraint holds.
    data = {"dexterity": 15, "xp": 350}
    result = validate_and_prepare_write(definition, data)
    assert result == data  # derived is never persisted onto the write payload

    from core.entities.validation import compute_derived

    derived = compute_derived(definition, data)
    assert derived == {"level": 3}

    # Non-compliant write: constraint violation names the failing predicate.
    try:
        validate_and_prepare_write(definition, {"dexterity": 15, "xp": 50})
        raise AssertionError("expected ConstraintViolationError")
    except ConstraintViolationError as exc:
        assert exc.expression == "fields.dexterity >= 13 && fields.level >= 3"

    # A bad field value is also rejected.
    try:
        validate_and_prepare_write(definition, {"dexterity": "not a number", "xp": 50})
        raise AssertionError("expected FieldValidationError")
    except FieldValidationError:
        pass


async def test_rpg_and_ticket_schemas_use_identical_core_paths(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """No core branching on domain: the exact same ``save_schema``/
    ``validate_and_prepare_write`` calls express an RPG attribute block and a ticket
    record."""
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)

    rpg_definition = EntitySchemaDefinition(
        fields=[
            FieldDef(key="strength", type="integer", minimum=1, maximum=20),
            FieldDef(key="hit_points", type="integer", minimum=0),
        ],
        derived=[
            DerivedDef(key="modifier", type="integer", expression="(fields.strength - 10) / 2")
        ],
    )
    ticket_definition = EntitySchemaDefinition(
        fields=[
            FieldDef(key="status", type="string", enum=["draft", "in_review", "approved"]),
            FieldDef(key="budget_remaining", type="integer", minimum=0),
        ],
        derived=[
            DerivedDef(
                key="is_over_budget", type="boolean", expression="fields.budget_remaining < 0"
            )
        ],
    )

    rpg_row = await save_schema(tenant_a, workspace_id, "rpg-char", 1, rpg_definition)
    ticket_row = await save_schema(tenant_a, workspace_id, "ticket", 1, ticket_definition)

    rpg_write = validate_and_prepare_write(rpg_definition, {"strength": 16, "hit_points": 12})
    ticket_write = validate_and_prepare_write(
        ticket_definition, {"status": "draft", "budget_remaining": 100}
    )
    assert rpg_write["strength"] == 16
    assert ticket_write["status"] == "draft"
    assert rpg_row.key != ticket_row.key
    assert rpg_row.tenant_id == ticket_row.tenant_id == tenant_a


async def test_entity_schema_cross_tenant_filter_omission_returns_zero_rows(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    definition = EntitySchemaDefinition(fields=[FieldDef(key="name", type="string")])

    for tenant_id in (tenant_a, tenant_b):
        workspace_id = await _workspace_id(tenant_id)
        await save_schema(tenant_id, workspace_id, "cross-tenant-schema", 1, definition)

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM entity_schema"))).all()

    assert {row[0] for row in rows} == {tenant_a}


async def test_list_schema_versions_returns_every_version_newest_first(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """F3.11's version history panel needs every version of a key, not just the
    latest -- ``list_latest_schemas`` deliberately collapses to one row per key, so
    this is a distinct query, exercised here against real save-time versioning."""
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)
    definition = EntitySchemaDefinition(fields=[FieldDef(key="name", type="string")])

    for version in (1, 2, 3):
        await save_schema(tenant_a, workspace_id, "versioned-schema", version, definition)

    versions = await list_schema_versions(tenant_a, workspace_id, "versioned-schema")
    assert [row.version for row in versions] == [3, 2, 1]
