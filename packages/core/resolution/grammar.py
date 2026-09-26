"""Randomizer-expression grammar + parser. Deliberately generic, not
d20-specific: ``NdM[kK][+-]MOD...`` where ``M`` (die sides) has no fixed value set here --
a coin flip is just ``1d2``, so "a coin-flip system must be expressible" falls out of the
same grammar rather than needing a special case. Which sides/counts/keep-syntax are
actually *legal* is a per-``RuleSystem`` policy (``expression_grammar`` config), checked by
``core.resolution.validate``, not by this module -- this module only decides whether a
string is syntactically a randomizer expression at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_EXPR_RE = re.compile(
    r"^(?P<count>\d+)d(?P<sides>\d+)(?:k(?P<keep>\d+))?(?P<mods>(?:[+-](?:\d+|[A-Za-z_][A-Za-z0-9_]*))*)$"
)
_MOD_TERM_RE = re.compile(r"(?P<sign>[+-])(?P<term>\d+|[A-Za-z_][A-Za-z0-9_]*)")

# A sanity bound, not a rule-system policy: no legitimate expression needs more terms than
# this, and without it a fuzzer/adversarial caller could ask for an absurd allocation
# further down the pipeline (the actual rolling).
_MAX_COUNT = 1000


class GrammarError(Exception):
    """``expression`` isn't a syntactically valid randomizer expression at all -- distinct from
    ``validate()``'s rule-system-specific legality errors (illegal sides, unknown
    modifier symbol, ...), which need a successfully *parsed* expression to check."""


@dataclass(frozen=True)
class ModifierTerm:
    sign: Literal["+", "-"]
    value: int | None
    symbol: str | None


@dataclass(frozen=True)
class ParsedExpression:
    raw: str
    count: int
    sides: int
    keep: int | None
    modifiers: tuple[ModifierTerm, ...]


def parse_expression(expression: str) -> ParsedExpression:
    """Raises ``GrammarError`` on anything malformed -- never any other exception type,
    so a caller (including a fuzz test) can catch exactly one thing."""
    match = _EXPR_RE.match(expression.strip())
    if match is None:
        raise GrammarError(f"{expression!r} is not a valid randomizer expression")

    count = int(match.group("count"))
    sides = int(match.group("sides"))
    keep = int(match.group("keep")) if match.group("keep") is not None else None

    if count == 0 or sides == 0:
        raise GrammarError(f"{expression!r}: term count and sides must be positive")
    if count > _MAX_COUNT:
        raise GrammarError(f"{expression!r}: term count {count} exceeds the sanity bound")
    if keep is not None and keep > count:
        raise GrammarError(f"{expression!r}: cannot keep {keep} of {count} terms")

    modifiers = tuple(
        ModifierTerm(
            sign=m.group("sign"),  # type: ignore[arg-type]
            value=int(m.group("term")) if m.group("term").isdigit() else None,
            symbol=None if m.group("term").isdigit() else m.group("term"),
        )
        for m in _MOD_TERM_RE.finditer(match.group("mods"))
    )

    return ParsedExpression(
        raw=expression, count=count, sides=sides, keep=keep, modifiers=modifiers
    )
