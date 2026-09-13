"""CEL compile-check for EntitySchema derived fields and constraints (F3.1, D7, §10.2).

Mirrors ``core.process.dsl.cel``'s real-evaluation-not-just-syntax approach: celpy's
``Environment.compile()`` only catches *syntax* errors, so this module also evaluates the
compiled program once against a synthetic ``fields.<declared field>`` activation built from
the schema's own declarations (raw fields keyed by their JSON-Schema-subset type, derived
fields keyed by their own declared type -- a constraint may legally reference a derived
field, e.g. ``fields.dexterity >= 13 && fields.level >= 3``) -- an undeclared reference
surfaces as a ``CELEvalError`` from that evaluation, treated identically to a compile error.

Not a full type-checker, same caveat ``core.process.dsl.cel`` documents: a runtime
divide-by-zero or similar value-dependent misbehaviour is out of scope for a static
validator using dummy values.
"""

from __future__ import annotations

import celpy
from celpy.adapter import json_to_cel
from celpy.celparser import CELParseError
from celpy.evaluation import CELEvalError

_env = celpy.Environment()

_DUMMY_VALUE_BY_TYPE: dict[str, object] = {
    "integer": 1,
    "number": 1.0,
    "string": "x",
    "boolean": True,
    "array": [],
}


class CELValidationError(Exception):
    """A CEL expression failed to compile or referenced something undeclared."""


def dummy_fields(field_types: dict[str, str]) -> dict[str, object]:
    return {name: _DUMMY_VALUE_BY_TYPE[type_] for name, type_ in field_types.items()}


def compile_check(source: str, field_types: dict[str, str]) -> None:
    """Raises ``CELValidationError`` if ``source`` doesn't parse, or references anything
    outside ``fields.<declared field>`` plus CEL's built-in operators/literals."""
    try:
        ast = _env.compile(source)
    except CELParseError as exc:
        raise CELValidationError(f"CEL syntax error in {source!r}: {exc}") from exc

    try:
        program = _env.program(ast)
        activation = json_to_cel({"fields": dummy_fields(field_types)})
        # Same celpy stub gap core.process.dsl.cel documents: json_to_cel's return type
        # isn't aligned with what Runner.evaluate() declares, even though a dict input
        # always produces a MapType (a dict subclass) at runtime.
        program.evaluate(activation)  # type: ignore[arg-type]
    except CELEvalError as exc:
        raise CELValidationError(f"CEL evaluation error in {source!r}: {exc}") from exc


def evaluate(source: str, fields: dict[str, object]) -> object:
    """Real evaluation against real field data (entity write path). Returns celpy's own
    result type uncoerced -- same convention as ``core.process.interpreter._eval_cel``:
    the caller re-types the result against its own declared type (celpy's ``BoolType``
    subclasses Python ``int``, so a raw celpy value is not safe to store or compare
    without that explicit step)."""
    try:
        program = _env.program(_env.compile(source))
        activation = json_to_cel({"fields": fields})
        return program.evaluate(activation)  # type: ignore[arg-type]
    except (CELParseError, CELEvalError) as exc:
        raise CELValidationError(f"CEL evaluation error in {source!r}: {exc}") from exc
