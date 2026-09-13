"""B1.1 acceptance criterion: "A definition that validates cannot make the interpreter
throw on structural grounds (fuzz/property test over generated valid definitions)."

B1.2's interpreter doesn't exist yet, so this can't literally run one. What it proves
instead -- the thing that actually determines whether the interpreter can trust its
input -- is that the validator's structural guarantees hold across a wide, randomly
generated space of definitions, not just the two hand-written fixtures: every phase the
graph can reach is declared, every transition target is a declared phase, the initial
phase exists, and (for phases that have one) every budget's ratios sum to ~1. Those are
exactly the properties B1.2's loop (``session.phases[current_phase]``, ``phase.gates[i]
.to``, ``split_budget(phase.budget.ratio, ...)``) would otherwise have to defensively
re-check or risk a ``KeyError``/malformed-budget fault on a definition the validator
already accepted.

The strategy below generates a random *chain* of phases (guaranteeing reachability by
construction: phase i's only transition points to phase i+1) with a few random back-edges
added as extra gates (exercising real cycles, matching the fixtures) -- not a
fully-unconstrained random JSON generator, since that would mostly produce documents
Pydantic itself rejects before validator.py ever runs, testing celpy/Pydantic's own error
handling rather than this module's.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.dsl.validator import validate_definition

_PHASE_NAME_ALPHABET = "abcdefghijklmnopqrstuvwxyz_"


def _phase_names(n: int) -> st.SearchStrategy[list[str]]:
    return st.lists(
        st.text(alphabet=_PHASE_NAME_ALPHABET, min_size=3, max_size=12),
        min_size=n,
        max_size=n,
        unique=True,
    )


@st.composite
def _valid_definition(draw: st.DrawFn) -> dict[str, object]:
    n_phases = draw(st.integers(min_value=2, max_value=8))
    names = draw(_phase_names(n_phases))

    has_round = draw(st.booleans())
    state: dict[str, object] = {}
    if has_round:
        state["round"] = {"type": "integer", "default": 0}

    phases: dict[str, object] = {}
    for i, name in enumerate(names):
        knowledge_classes = draw(st.lists(st.sampled_from(["rules", "lore", "misc"]), max_size=3))
        visibility = {
            "knowledge_classes": knowledge_classes,
            "scopes": ["workspace_public"],
            "entity_fields": "all",
            "secrets": "none",
        }
        include_budget = draw(st.booleans())
        budget = None
        if include_budget:
            # Two shares that sum to exactly 1.0 -- avoids flaky floating-point failures
            # of the ~1.0 tolerance check for a property that isn't what this test is about.
            share = draw(st.integers(min_value=1, max_value=99)) / 100
            budget = {"ratio": {"rules": share, "lore": 1 - share}, "max_tokens": 1000}

        is_last = i == len(names) - 1
        gates = []
        on_complete = None
        if is_last:
            pass  # terminal phase: no outgoing transition
        elif draw(st.booleans()):
            on_complete = names[i + 1]
        else:
            gates = [{"else": True, "to": names[i + 1]}]
            # Occasional back-edge for real cycle coverage, guarded by a declared state var
            # so the CEL compile-check has something real to validate against.
            if has_round and i > 0 and draw(st.booleans()):
                gates.insert(0, {"when": "state.round > 0", "to": names[0]})

        phases[name] = {
            "label_key": f"phase.{name}",
            "actors": [{"persona_type": "supervisor", "mode": "generate"}],
            "visibility": visibility,
            "budget": budget,
            "gates": gates,
            "on_complete": on_complete,
        }

    return {
        "name": "Property-generated flow",
        "vocabulary_overlay": "rpg_v1",
        "state": state,
        "initial_phase": names[0],
        "phases": phases,
    }


@given(_valid_definition())
@settings(max_examples=200)
def test_generated_valid_definitions_always_pass_validation(raw: dict[str, object]) -> None:
    dsl = ProcessDefinitionDSL.model_validate(raw)  # must not raise -- generator invariant
    issues = validate_definition(dsl)
    assert issues == [], f"generator produced a definition validate_definition rejected: {issues}"


@given(_valid_definition())
@settings(max_examples=200)
def test_every_transition_target_in_a_valid_definition_is_a_real_phase(
    raw: dict[str, object],
) -> None:
    """The specific structural guarantee B1.2's loop leans on hardest: dereferencing a
    gate's ``to`` or a phase's ``on_complete`` by key must never KeyError."""
    dsl = ProcessDefinitionDSL.model_validate(raw)
    assert validate_definition(dsl) == []

    for phase in dsl.phases.values():
        if phase.on_complete is not None:
            assert phase.on_complete in dsl.phases
        for gate in phase.gates:
            assert gate.to in dsl.phases


@given(_valid_definition())
@settings(max_examples=200)
def test_every_phase_with_a_budget_has_ratios_summing_to_one(raw: dict[str, object]) -> None:
    dsl = ProcessDefinitionDSL.model_validate(raw)
    assert validate_definition(dsl) == []

    for phase in dsl.phases.values():
        if phase.budget is not None:
            assert abs(sum(phase.budget.ratio.values()) - 1.0) < 1e-9
