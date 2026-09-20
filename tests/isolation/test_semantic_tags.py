"""F3.4's own acceptance tests: the closed tag vocabulary, the tag->widget registry's
totality/domain-blindness, and cross-domain resource-widget resolution.
"""

from __future__ import annotations

import inspect
import uuid

from sqlalchemy import select

from core.entities.repo import save_schema
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.tags import SEMANTIC_TAGS, TAG_WIDGET_REGISTRY, validate_tags, widget_for
from core.entities.validation import SchemaValidationError
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope

# CLAUDE.md's forbidden-vocabulary list, spot-checked here: a domain-blind registry
# must not mention any of these, core or RPG/enterprise/swdev pack nouns alike.
_FORBIDDEN_DOMAIN_WORDS = (
    "game_master",
    "gm",
    "dice",
    "campaign",
    "character",
    "player",
    "spell",
    "npc",
    "hit_points",
    "ticket",
    "work_item",
    "sprint",
    "standup",
)


async def _workspace_id(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def test_unknown_semantic_tag_fails_schema_validation(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)

    definition = EntitySchemaDefinition(
        fields=[FieldDef(key="mana", type="integer", tags=["resource", "mystical_energy"])]
    )
    try:
        await save_schema(tenant_a, workspace_id, "unknown-tag-schema", 1, definition)
        raise AssertionError("expected SchemaValidationError")
    except SchemaValidationError as exc:
        messages = [f"{i.field_path}: {i.message}" for i in exc.issues]
        assert any("mystical_energy" in m and "mana" in m for m in messages)


def test_tag_widget_registry_is_total_and_domain_blind() -> None:
    assert set(TAG_WIDGET_REGISTRY) == SEMANTIC_TAGS
    for tag in SEMANTIC_TAGS:
        assert widget_for(tag)  # every tag resolves to a non-empty widget id

    # "Domain-blind" is a claim about the registry's own data (and the validator logic
    # keyed off it) -- not about illustrative examples in prose docstrings/comments,
    # which this codebase's own core modules use freely elsewhere (e.g.
    # core.resolution.rule_system's docstring names the shipped systems and "d20").
    registry_literals = " ".join(f"{k} {v}" for k, v in TAG_WIDGET_REGISTRY.items()).lower()
    offenders = [word for word in _FORBIDDEN_DOMAIN_WORDS if word in registry_literals]
    assert not offenders, f"tag->widget registry references domain content: {offenders}"

    validate_tags_source = inspect.getsource(validate_tags).lower()
    code_offenders = [word for word in _FORBIDDEN_DOMAIN_WORDS if word in validate_tags_source]
    assert not code_offenders, f"validate_tags references domain content: {code_offenders}"


async def test_three_domains_resolve_resource_to_the_same_widget(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)

    hp_field = FieldDef(
        key="hit_points",
        type="integer",
        tags=["resource"],
        tag_metadata={"max_ref": "max_hit_points", "low_threshold": 5},
    )
    budget_field = FieldDef(
        key="budget_remaining",
        type="number",
        tags=["resource"],
        tag_metadata={"max_ref": "total_budget", "low_threshold": 1000.0},
    )
    estimate_field = FieldDef(
        key="remaining_estimate",
        type="number",
        tags=["resource"],
        tag_metadata={"max_ref": "original_estimate", "low_threshold": 1.0},
    )

    definitions = [
        EntitySchemaDefinition(fields=[hp_field, FieldDef(key="max_hit_points", type="integer")]),
        EntitySchemaDefinition(fields=[budget_field, FieldDef(key="total_budget", type="number")]),
        EntitySchemaDefinition(
            fields=[estimate_field, FieldDef(key="original_estimate", type="number")]
        ),
    ]
    for i, definition in enumerate(definitions):
        await save_schema(tenant_a, workspace_id, f"resource-fixture-{i}", 1, definition)

    widgets = {
        widget_for(tag) for field in (hp_field, budget_field, estimate_field) for tag in field.tags
    }
    assert widgets == {"resource_bar"}  # identical lookup, metadata values differ only
