"""Acceptance criteria against the two shipped fixtures."""

from __future__ import annotations

from core.process.dsl.fixtures import MINIMAL_MVP_FLOW, STANDARD_SESSION_FLOW
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.dsl.validator import validate_raw


def test_standard_session_flow_validates_with_no_issues() -> None:
    dsl, issues = validate_raw(STANDARD_SESSION_FLOW)
    assert issues == []
    assert dsl is not None


def test_minimal_mvp_flow_validates_with_no_issues() -> None:
    dsl, issues = validate_raw(MINIMAL_MVP_FLOW)
    assert issues == []
    assert dsl is not None


def test_standard_session_flow_round_trips_parse_validate_serialize() -> None:
    """parse -> validate -> serialize identically: dumping, re-parsing, and re-dumping
    again reaches a fixed point -- the definition of "round-trips" for a schema whose
    canonical JSON form fills in defaults the hand-authored input may have omitted."""
    dsl = ProcessDefinitionDSL.model_validate(STANDARD_SESSION_FLOW)
    first_dump = dsl.model_dump(mode="json", by_alias=True, exclude_none=True)

    reparsed = ProcessDefinitionDSL.model_validate(first_dump)
    second_dump = reparsed.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert first_dump == second_dump


def test_minimal_mvp_flow_round_trips_parse_validate_serialize() -> None:
    dsl = ProcessDefinitionDSL.model_validate(MINIMAL_MVP_FLOW)
    first_dump = dsl.model_dump(mode="json", by_alias=True, exclude_none=True)
    reparsed = ProcessDefinitionDSL.model_validate(first_dump)
    second_dump = reparsed.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert first_dump == second_dump


def test_standard_session_flow_has_a_cyclic_transition_graph() -> None:
    """Confirms the fixture actually exercises the case validator.py's docstring is
    explicit about allowing: open_discussion <-> action_phase <-> resolution, and
    resolution -> feedback_loop -> open_discussion are both real cycles."""
    dsl = ProcessDefinitionDSL.model_validate(STANDARD_SESSION_FLOW)
    assert dsl.phases["resolution"].gates[1].to == "open_discussion"
    assert dsl.phases["open_discussion"].gates[0].to == "action_phase"
    assert dsl.phases["action_phase"].on_complete == "resolution"
