"""C1.5 acceptance criteria for RuleSystem storage, authoring validation, and outcome
banding, against a live Postgres.
"""

from __future__ import annotations

import uuid

import pydantic
import pytest

from core.resolution.rule_system import (
    COIN_FLIP_SYSTEM,
    MINIMAL_D20_SYSTEM,
    OutcomeBandingError,
    RuleSystemDefinition,
    RuleSystemDefinitionSchema,
    RuleSystemValidationError,
    create_rule_system,
    get_or_create_default_rule_system,
    get_rule_system,
    resolve_outcome,
    validate_definition,
)
from core.tenancy.seed import seed_dev_tenant


async def test_create_and_get_rule_system_round_trips(db_available: None) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"rulesys-crud-{uuid.uuid4().hex[:8]}"
    )
    row = await create_rule_system(tenant_id, MINIMAL_D20_SYSTEM)
    assert row.key == "mvp_d20"

    fetched = await get_rule_system(tenant_id, "mvp_d20")
    assert fetched is not None
    assert fetched.id == row.id
    assert fetched.modifier_resolver == MINIMAL_D20_SYSTEM.modifier_resolver


async def test_create_rule_system_is_an_upsert_by_key(db_available: None) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"rulesys-upsert-{uuid.uuid4().hex[:8]}"
    )
    first = await create_rule_system(tenant_id, MINIMAL_D20_SYSTEM)
    updated_schema = MINIMAL_D20_SYSTEM.model_copy(update={"name": "Renamed System"})
    second = await create_rule_system(tenant_id, updated_schema)

    assert first.id == second.id
    assert second.name == "Renamed System"


async def test_get_or_create_default_rule_system_is_idempotent(db_available: None) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"rulesys-default-{uuid.uuid4().hex[:8]}"
    )
    first = await get_or_create_default_rule_system(tenant_id)
    second = await get_or_create_default_rule_system(tenant_id)

    assert first.id == second.id
    assert first.key == "mvp_d20"


async def test_two_different_rule_systems_coexist_per_tenant(db_available: None) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"rulesys-coexist-{uuid.uuid4().hex[:8]}"
    )
    await create_rule_system(tenant_id, MINIMAL_D20_SYSTEM)
    await create_rule_system(tenant_id, COIN_FLIP_SYSTEM)

    d20 = await get_rule_system(tenant_id, "mvp_d20")
    coin = await get_rule_system(tenant_id, "coin_flip")
    assert d20 is not None
    assert coin is not None
    assert d20.id != coin.id


# ── authoring-time validation ────────────────────────────────────────────────────────


def test_validate_definition_accepts_the_fixtures() -> None:
    validate_definition(MINIMAL_D20_SYSTEM)
    validate_definition(COIN_FLIP_SYSTEM)


def test_validate_definition_rejects_bad_cel_syntax() -> None:
    bad = MINIMAL_D20_SYSTEM.model_copy(
        update={"modifier_resolver": {"stealth": "fields.dexterity - -"}}
    )
    with pytest.raises(RuleSystemValidationError):
        validate_definition(bad)


def test_validate_definition_rejects_resolver_for_unknown_check_type() -> None:
    bad = MINIMAL_D20_SYSTEM.model_copy(
        update={
            "modifier_resolver": {
                **MINIMAL_D20_SYSTEM.modifier_resolver,
                "nonexistent_check": "0",
            }
        }
    )
    with pytest.raises(RuleSystemValidationError):
        validate_definition(bad)


def test_definition_schema_forbids_unknown_fields() -> None:
    with pytest.raises(pydantic.ValidationError):
        RuleSystemDefinitionSchema(
            key="x",
            name="X",
            expression_grammar={},
            check_types=[],
            modifier_resolver={},
            not_a_real_field="oops",  # type: ignore[call-arg]
        )


# ── outcome banding ──────────────────────────────────────────────────────────────────


def test_resolve_outcome_target_based_threshold() -> None:
    assert resolve_outcome(19, target=15, outcome_bands=()) == "success"
    assert resolve_outcome(10, target=15, outcome_bands=()) == "failure"


def test_resolve_outcome_banded_thresholds() -> None:
    bands = (
        {"min": 10, "max": None, "outcome": "full_success"},
        {"min": 7, "max": 9, "outcome": "partial_success"},
        {"min": None, "max": 6, "outcome": "failure"},
    )
    assert resolve_outcome(12, target=None, outcome_bands=bands) == "full_success"
    assert resolve_outcome(8, target=None, outcome_bands=bands) == "partial_success"
    assert resolve_outcome(3, target=None, outcome_bands=bands) == "failure"


def test_resolve_outcome_coin_flip_bands() -> None:
    bands = COIN_FLIP_SYSTEM.outcome_bands
    assert resolve_outcome(1, target=None, outcome_bands=tuple(bands)) == "tails"
    assert resolve_outcome(2, target=None, outcome_bands=tuple(bands)) == "heads"


def test_resolve_outcome_raises_when_no_band_matches_and_no_target() -> None:
    with pytest.raises(OutcomeBandingError):
        resolve_outcome(99, target=None, outcome_bands=({"min": 1, "max": 2, "outcome": "x"},))


def test_definition_from_row_and_from_schema_agree() -> None:
    from_schema = RuleSystemDefinition.from_schema(MINIMAL_D20_SYSTEM)
    assert from_schema.key == "mvp_d20"
    assert from_schema.check_types == frozenset(MINIMAL_D20_SYSTEM.check_types)
