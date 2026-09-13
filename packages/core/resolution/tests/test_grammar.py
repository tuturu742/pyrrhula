"""C1.5 acceptance criteria for the dice-expression grammar/parser."""

from __future__ import annotations

import contextlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.resolution.grammar import GrammarError, ModifierTerm, parse_expression


def test_parses_a_plain_expression() -> None:
    parsed = parse_expression("1d20")
    assert parsed.count == 1
    assert parsed.sides == 20
    assert parsed.keep is None
    assert parsed.modifiers == ()


def test_parses_a_literal_modifier() -> None:
    parsed = parse_expression("1d20+5")
    assert parsed.modifiers == (ModifierTerm(sign="+", value=5, symbol=None),)


def test_parses_a_symbolic_modifier() -> None:
    parsed = parse_expression("2d6+STR")
    assert parsed.count == 2
    assert parsed.sides == 6
    assert parsed.modifiers == (ModifierTerm(sign="+", value=None, symbol="STR"),)


def test_parses_multiple_modifiers_mixed_literal_and_symbolic() -> None:
    parsed = parse_expression("1d20-2+DEX")
    assert parsed.modifiers == (
        ModifierTerm(sign="-", value=2, symbol=None),
        ModifierTerm(sign="+", value=None, symbol="DEX"),
    )


def test_parses_keep_syntax() -> None:
    parsed = parse_expression("4d6k3")
    assert parsed.count == 4
    assert parsed.sides == 6
    assert parsed.keep == 3


def test_coin_flip_is_expressible_as_plain_d2() -> None:
    """A coin flip is just 1d2 -- no special grammar case needed."""
    parsed = parse_expression("1d2")
    assert parsed.count == 1
    assert parsed.sides == 2


def test_zero_count_or_sides_rejected() -> None:
    for bad in ("0d20", "1d0"):
        with pytest.raises(GrammarError):
            parse_expression(bad)


def test_keeping_more_than_rolled_rejected() -> None:
    with pytest.raises(GrammarError):
        parse_expression("2d6k5")


def test_garbage_input_raises_grammar_error_not_something_else() -> None:
    for bad in ("", "not a dice roll", "1d20++5", "d20", "1d", "1d20+", "1d20*5"):
        with pytest.raises(GrammarError):
            parse_expression(bad)


# ── fuzz: invalid expressions never crash, always a typed GrammarError ─────────────


@given(st.text(min_size=0, max_size=40))
def test_fuzz_never_raises_anything_but_grammar_error(candidate: str) -> None:
    with contextlib.suppress(GrammarError):  # the only acceptable failure mode
        parse_expression(candidate)
