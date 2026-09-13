"""CEL compile-check (B1.1, plan §5.2/§5.3): every ``gates[].when`` and ``effects[].to``
expression is checked at save time so a bad expression is an authoring-time error, never
an interpreter-time fault (B1.2).

celpy's ``Environment.compile()`` only catches *syntax* errors -- referencing an
undeclared name (anything other than ``state.<declared var>`` plus CEL's own operators/
literals) parses fine and only fails at evaluation time (confirmed empirically: celpy has
no static type-checking pass without a full declared-environment setup this codebase
doesn't otherwise need). So this module compiles the expression, then evaluates it once
against a synthetic activation built from the DSL's own declared ``state:`` block (a
representative dummy value per declared type) -- an undeclared reference surfaces as a
`CELEvalError` from *that* evaluation, which this module treats identically to a syntax
error: both mean "the interpreter cannot trust this expression."

This is a real check, not merely a syntax check, but it is not a full type-checker: a CEL
expression whose runtime behaviour depends on the *actual* value of a state variable
(not just its type) at a specific evaluation could still misbehave in ways this dummy
evaluation can't catch (e.g. divide-by-zero only when a var is exactly 0). Out of scope
for a static validator; the interpreter's own error handling (B1.2: "an interpreter fault
pauses the session, never a stuck lock") is the backstop for that class of failure.
"""

from __future__ import annotations

import celpy
from celpy.adapter import json_to_cel
from celpy.celparser import CELParseError
from celpy.evaluation import CELEvalError

from core.process.dsl.schema import StateVarSpec

_env = celpy.Environment()

_DUMMY_VALUE_BY_TYPE: dict[str, object] = {
    "integer": 1,
    "number": 1.0,
    "string": "x",
    "boolean": True,
}


class CELValidationError(Exception):
    """A CEL expression failed to compile or referenced something undeclared."""


def _dummy_state(state_vars: dict[str, StateVarSpec]) -> dict[str, object]:
    return {name: _DUMMY_VALUE_BY_TYPE[spec.type] for name, spec in state_vars.items()}


def compile_check(source: str, state_vars: dict[str, StateVarSpec]) -> None:
    """Raises ``CELValidationError`` if ``source`` doesn't parse, or references anything
    outside ``state.<declared var>`` plus CEL's built-in operators/literals."""
    try:
        ast = _env.compile(source)
    except CELParseError as exc:
        raise CELValidationError(f"CEL syntax error in {source!r}: {exc}") from exc

    try:
        program = _env.program(ast)
        activation = json_to_cel({"state": _dummy_state(state_vars)})
        # celpy's own stubs don't align json_to_cel's return type with what
        # Runner.evaluate() declares it wants, even though a dict input to json_to_cel
        # always produces a MapType (a dict subclass) at runtime, confirmed empirically
        # to work -- a celpy stub gap, not a real type error in this call.
        program.evaluate(activation)  # type: ignore[arg-type]
    except CELEvalError as exc:
        raise CELValidationError(f"CEL evaluation error in {source!r}: {exc}") from exc
