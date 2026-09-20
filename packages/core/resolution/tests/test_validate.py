"""C1.5 acceptance criteria for the anti-hallucination validator itself (plan §9.2 step
2/§9.3). No live DB needed -- ``validate()`` is a pure function over a
``RuleSystemDefinition`` and a caller-supplied ``actor_fields`` dict.
"""

from __future__ import annotations

from core.resolution.rule_system import COIN_FLIP_SYSTEM, MINIMAL_D20_SYSTEM, RuleSystemDefinition
from core.resolution.validate import validate

_D20 = RuleSystemDefinition.from_schema(MINIMAL_D20_SYSTEM)
_COIN = RuleSystemDefinition.from_schema(COIN_FLIP_SYSTEM)


# ── the literal acceptance criterion: claimed modifier vs actual ────────────────────


def test_claimed_modifier_mismatching_the_actors_actual_modifier_is_rejected() -> None:
    """dexterity=16 -> (16-10)/2 = +3. The model claims +5."""
    result = validate("1d20+5", "stealth", {"dexterity": 16}, _D20, legal_check_types=None)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "modifier_mismatch"
    assert result.error.expected_modifier == 3
    assert "3" in result.error.message  # names the correct modifier, not just "wrong"


def test_claimed_modifier_matching_the_actual_modifier_is_accepted() -> None:
    result = validate("1d20+3", "stealth", {"dexterity": 16}, _D20, legal_check_types=None)
    assert result.ok is True
    assert result.computed_modifier == 3


def test_symbolic_modifier_has_nothing_to_cross_check_but_still_resolves() -> None:
    """`2d6+STR` has no literal claim -- the computed modifier is still authoritative and
    returned, just not compared against anything."""
    d20_with_symbolic_check = RuleSystemDefinition(
        key="mvp_d20",
        expression_grammar={"allowed_sides": [6], "max_term_count": 4, "allow_keep_drop": False},
        check_types=frozenset({"strength_check"}),
        modifier_resolver={"strength_check": "(fields.strength - 10) / 2"},
    )
    result = validate(
        "2d6+STR",
        "strength_check",
        {"strength": 14},
        d20_with_symbolic_check,
        legal_check_types=None,
    )
    assert result.ok is True
    assert result.computed_modifier == 2


# ── legality checks ──────────────────────────────────────────────────────────────────


def test_illegal_sides_rejected() -> None:
    result = validate("1d13+0", "stealth", {"dexterity": 10}, _D20, legal_check_types=None)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "illegal_expression"


def test_too_many_terms_rejected() -> None:
    result = validate("10d6", "stealth", {"dexterity": 10}, _D20, legal_check_types=None)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "illegal_expression"


def test_keep_syntax_rejected_when_rule_system_disallows_it() -> None:
    result = validate("4d6k3", "stealth", {"dexterity": 10}, _D20, legal_check_types=None)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "illegal_expression"


def test_unknown_check_type_rejected() -> None:
    result = validate("1d20+0", "arcana", {"dexterity": 10}, _D20, legal_check_types=None)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "unknown_check_type"


def test_check_not_legal_in_current_phase_rejected() -> None:
    result = validate(
        "1d20+0",
        "stealth",
        {"dexterity": 10},
        _D20,
        legal_check_types=frozenset({"strength_check"}),  # stealth not among them
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "check_not_legal_in_phase"


def test_check_legal_in_phase_is_accepted() -> None:
    result = validate(
        "1d20+0", "stealth", {"dexterity": 10}, _D20, legal_check_types=frozenset({"stealth"})
    )
    assert result.ok is True


def test_garbage_expression_never_crashes_always_typed_error() -> None:
    result = validate("not an expression", "stealth", {}, _D20, legal_check_types=None)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "illegal_expression"


# ── the same validator code path accepts a d20 system AND a coin-flip system ────────


def test_same_validate_function_handles_d20_and_coin_flip_with_no_branching() -> None:
    d20_result = validate("1d20+3", "stealth", {"dexterity": 16}, _D20, legal_check_types=None)
    coin_result = validate("1d2+0", "call", {}, _COIN, legal_check_types=None)

    assert d20_result.ok is True
    assert coin_result.ok is True
    # Both went through the exact same `validate()` -- no rule_system.key branching
    # anywhere in this test or in validate() itself.


def test_coin_flip_wrong_sides_rejected_by_its_own_grammar() -> None:
    result = validate("1d6+0", "call", {}, _COIN, legal_check_types=None)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "illegal_expression"


# ── custom validators (CEL predicates) ───────────────────────────────────────────────


def test_custom_validator_predicate_can_reject_an_otherwise_legal_expression() -> None:
    rule_system = RuleSystemDefinition(
        key="strict",
        expression_grammar={"allowed_sides": [20], "max_term_count": 1, "allow_keep_drop": False},
        check_types=frozenset({"stealth"}),
        modifier_resolver={"stealth": "(fields.dexterity - 10) / 2"},
        validators=("fields.dexterity <= 20",),
    )
    # dexterity=30 -> computed modifier (30-10)/2 = 10, claimed +10 matches (so the
    # modifier check passes) -- the custom validator is what should reject this one.
    result = validate("1d20+10", "stealth", {"dexterity": 30}, rule_system, legal_check_types=None)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "validator_failed"
