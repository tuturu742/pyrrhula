"""structural (Pydantic-level) schema constraints -- the layer that catches a bad
document one field at a time, before validator.py's whole-document graph/semantic checks
even run.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.process.dsl.schema import ActorSpec, GateSpec, StateVarSpec, parse_duration_seconds


def test_state_var_default_must_match_declared_type() -> None:
    StateVarSpec.model_validate({"type": "integer", "default": 5})
    with pytest.raises(ValidationError):
        StateVarSpec.model_validate({"type": "integer", "default": "not an int"})


def test_state_var_bool_is_not_accepted_as_integer_default() -> None:
    """bool is a subclass of int in Python -- must not silently pass an 'integer' check."""
    with pytest.raises(ValidationError):
        StateVarSpec.model_validate({"type": "integer", "default": True})


def test_state_var_boolean_type_rejects_non_bool_default() -> None:
    with pytest.raises(ValidationError):
        StateVarSpec.model_validate({"type": "boolean", "default": 1})


@pytest.mark.parametrize(
    "duration,expected_seconds",
    [("30s", 30), ("5m", 300), ("24h", 86400), ("2d", 172800)],
)
def test_parse_duration_seconds(duration: str, expected_seconds: int) -> None:
    assert parse_duration_seconds(duration) == expected_seconds


@pytest.mark.parametrize("bad_duration", ["24", "24x", "h24", "-1h", ""])
def test_parse_duration_seconds_rejects_malformed_input(bad_duration: str) -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration_seconds(bad_duration)


def test_actor_spec_rejects_two_selectors_at_once() -> None:
    with pytest.raises(ValidationError, match="at most one of"):
        ActorSpec.model_validate(
            {"persona_type": "supervisor", "any_of": ["human_participant"], "mode": "generate"}
        )


def test_actor_spec_rejects_no_selector_without_initiative_order() -> None:
    with pytest.raises(ValidationError, match="needs one of"):
        ActorSpec.model_validate({"mode": "generate"})


def test_actor_spec_allows_no_selector_with_initiative_order() -> None:
    spec = ActorSpec.model_validate(
        {"order": "initiative", "from": 'entity_field("initiative")', "mode": "generate"}
    )
    assert spec.persona_type is None
    assert spec.any_of is None


def test_actor_spec_rejects_unknown_agent_role() -> None:
    with pytest.raises(ValidationError, match="unknown persona_type"):
        ActorSpec.model_validate({"persona_type": "game_master", "mode": "generate"})


def test_actor_spec_rejects_unknown_any_of_token() -> None:
    with pytest.raises(ValidationError, match="unknown any_of token"):
        ActorSpec.model_validate({"any_of": ["wizard"], "mode": "free"})


def test_actor_spec_initiative_order_requires_from_field() -> None:
    with pytest.raises(ValidationError, match="requires a 'from' field"):
        ActorSpec.model_validate(
            {"persona_type": "participant", "order": "initiative", "mode": "generate"}
        )


def test_gate_spec_rejects_zero_conditions() -> None:
    with pytest.raises(ValidationError, match="exactly one of"):
        GateSpec.model_validate({"to": "next_phase"})


def test_gate_spec_rejects_two_conditions() -> None:
    with pytest.raises(ValidationError, match="exactly one of"):
        GateSpec.model_validate(
            {"on": "actor_declares_action", "when": "state.x", "to": "next_phase"}
        )


def test_gate_spec_rejects_unknown_event_name() -> None:
    with pytest.raises(ValidationError, match="unknown gate event"):
        GateSpec.model_validate({"on": "something_made_up", "to": "next_phase"})


def test_gate_spec_accepts_timeout_event() -> None:
    gate = GateSpec.model_validate({"on": "timeout(24h)", "to": "next_phase"})
    assert gate.on == "timeout(24h)"


def test_gate_spec_rejects_malformed_timeout_event() -> None:
    with pytest.raises(ValidationError, match="unknown gate event"):
        GateSpec.model_validate({"on": "timeout(nonsense)", "to": "next_phase"})
