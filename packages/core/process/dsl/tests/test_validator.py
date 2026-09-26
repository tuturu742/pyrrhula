"""Acceptance criterion: "Each validation rule has a failing fixture (missing
visibility, unreachable phase, bad CEL, dangling transition) with a precise, field-
addressed error." Built by mutating the known-valid MINIMAL_MVP_FLOW fixture one field at
a time -- each test proves exactly one rule fires, with the exact field path a UI
would need to highlight the offending part of the document.
"""

from __future__ import annotations

import copy

import pytest

from core.process.dsl.fixtures import MINIMAL_MVP_FLOW
from core.process.dsl.validator import validate_raw


@pytest.fixture
def base() -> dict[str, object]:
    return copy.deepcopy(MINIMAL_MVP_FLOW)


def test_missing_visibility_fails_with_precise_field_path(base: dict) -> None:
    del base["phases"]["arbiter_narrate"]["visibility"]

    dsl, issues = validate_raw(base)

    assert dsl is None
    assert len(issues) == 1
    assert issues[0].field_path == "phases.arbiter_narrate.visibility"
    assert "required" in issues[0].message.lower()


def test_unreachable_phase_fails_with_precise_field_path(base: dict) -> None:
    base["phases"]["orphan"] = copy.deepcopy(base["phases"]["resolve"])
    base["phases"]["orphan"]["on_complete"] = "arbiter_narrate"

    dsl, issues = validate_raw(base)

    assert dsl is not None  # structurally valid; only the semantic pass flags this
    assert len(issues) == 1
    assert issues[0].field_path == "phases.orphan"
    assert "unreachable" in issues[0].message


def test_bad_cel_in_effect_fails_with_precise_field_path(base: dict) -> None:
    base["phases"]["resolve"]["effects"] = [{"set": "round", "to": "state.round +"}]

    dsl, issues = validate_raw(base)

    assert dsl is not None
    assert len(issues) == 1
    assert issues[0].field_path == "phases.resolve.effects[0].to"
    assert "CEL syntax error" in issues[0].message


def test_bad_cel_in_gate_when_fails_with_precise_field_path(base: dict) -> None:
    base["phases"]["resolve"]["gates"] = [{"when": "state.round ===", "to": "arbiter_narrate"}]
    del base["phases"]["resolve"]["on_complete"]

    dsl, issues = validate_raw(base)

    assert dsl is not None
    assert len(issues) == 1
    assert issues[0].field_path == "phases.resolve.gates[0].when"


def test_dangling_transition_fails_with_precise_field_path(base: dict) -> None:
    base["phases"]["resolve"]["on_complete"] = "nonexistent_phase"

    dsl, issues = validate_raw(base)

    assert dsl is not None
    assert len(issues) == 1
    assert issues[0].field_path == "phases.resolve.on_complete"
    assert "nonexistent_phase" in issues[0].message


def test_dangling_await_on_timeout_target(base: dict) -> None:
    base["phases"]["player_act"]["await"] = {
        "type": "human_input",
        "timeout": "24h",
        "on_timeout": "nonexistent_phase",
    }

    dsl, issues = validate_raw(base)

    assert dsl is not None
    assert any(i.field_path == "phases.player_act.await.on_timeout" for i in issues)


def test_initial_phase_not_declared(base: dict) -> None:
    base["initial_phase"] = "does_not_exist"

    dsl, issues = validate_raw(base)

    assert dsl is not None
    assert any(i.field_path == "initial_phase" for i in issues)


def test_budget_ratios_not_summing_to_one(base: dict) -> None:
    base["phases"]["arbiter_narrate"]["budget"] = {
        "ratio": {"rules": 0.3, "lore": 0.3},
        "max_tokens": 100,
    }

    dsl, issues = validate_raw(base)

    assert dsl is not None
    assert len(issues) == 1
    assert issues[0].field_path == "phases.arbiter_narrate.budget.ratio"
    assert "0.6" in issues[0].message


def test_budget_ratios_within_tolerance_pass(base: dict) -> None:
    base["phases"]["arbiter_narrate"]["budget"] = {
        "ratio": {"rules": 0.31, "lore": 0.68},
        "max_tokens": 100,
    }

    dsl, issues = validate_raw(base)

    assert issues == []


def test_gates_and_on_complete_together_is_rejected(base: dict) -> None:
    base["phases"]["resolve"]["gates"] = [{"else": True, "to": "arbiter_narrate"}]
    # on_complete is still set from the base fixture -- both mechanisms present.

    dsl, issues = validate_raw(base)

    assert dsl is not None
    assert any(
        i.field_path == "phases.resolve" and "gates or on_complete, not both" in i.message
        for i in issues
    )


def test_else_gate_not_last_is_rejected(base: dict) -> None:
    del base["phases"]["resolve"]["on_complete"]
    base["phases"]["resolve"]["gates"] = [
        {"else": True, "to": "arbiter_narrate"},
        {"when": "state.round > 0", "to": "player_act"},
    ]

    dsl, issues = validate_raw(base)

    assert dsl is not None
    assert any(i.field_path == "phases.resolve.gates[0]" for i in issues)


def test_valid_definition_produces_no_issues(base: dict) -> None:
    dsl, issues = validate_raw(base)
    assert dsl is not None
    assert issues == []
