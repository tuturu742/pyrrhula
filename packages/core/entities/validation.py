"""Validation for EntitySchema (save-time) and entity data (write-time).

Two distinct passes, mirroring ``core.process.dsl.validator``'s split:

* **Save-time** (``validate_schema_definition``): CEL compile-check on every derived
  field and constraint, against a dummy ``fields.*`` activation built from the schema's
  own declared field/derived types. A schema whose expressions don't compile is rejected
  with field-level errors before it ever reaches a row.
* **Write-time** (``validate_and_prepare_write``): real entity data validates against its
  schema version -- JSON-Schema-subset field checks (type/enum/range), then derived
  fields are computed (never stored as authored values -- design decision in
  ``core.entities.schema``), then constraints are checked against the *combined*
  raw+derived activation, naming the failing predicate on violation.

``validate_schema_definition`` also runs the ``core.entities.fsm.validate_state_
machines`` (reachability, dangling transitions, guard/effect CEL) and the
``core.entities.tags.validate_tags``/``core.entities.views.validate_views`` (unknown
tags, missing tag-metadata, dangling view references) -- one save-time entrypoint,
matching ``core.process.dsl.validator.validate_raw``'s "a caller never needs to know
which layer caught a given problem".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pydantic

from core.entities.cel import CELValidationError, compile_check, evaluate
from core.entities.fsm import validate_state_machines
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.tags import validate_tags
from core.entities.views import validate_views

COERCE_BY_TYPE = {
    "integer": int,
    "number": float,
    "string": str,
    "boolean": bool,
}


@dataclass(frozen=True)
class SchemaValidationIssue:
    field_path: str
    message: str


class SchemaValidationError(Exception):
    """Raised at save time by callers that want a single exception rather than the raw
    issue list (``core.entities.repo.save_schema``); carries the same issues."""

    def __init__(self, issues: list[SchemaValidationIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(f"{i.field_path}: {i.message}" for i in issues))


class FieldValidationError(Exception):
    """Write-time: a value doesn't satisfy its field's declared type/enum/range."""


class ConstraintViolationError(Exception):
    """Write-time: a constraint predicate evaluated false. Names the failing predicate
    (and optional authored message) -- never a generic "invalid data" error."""

    def __init__(self, expression: str, message: str | None) -> None:
        self.expression = expression
        self.message = message
        super().__init__(
            f"constraint violated: {expression}" + (f" ({message})" if message else "")
        )


def validate_schema_definition(definition: EntitySchemaDefinition) -> list[SchemaValidationIssue]:
    """Collects every CEL problem across derived/constraints (does not stop at the
    first) so a schema author sees the full set of failing expressions in one pass."""
    issues: list[SchemaValidationIssue] = []
    field_types = definition.field_types()

    for i, derived in enumerate(definition.derived):
        try:
            compile_check(derived.expression, field_types)
        except CELValidationError as exc:
            issues.append(SchemaValidationIssue(f"derived[{i}].expression", str(exc)))

    for i, constraint in enumerate(definition.constraints):
        try:
            compile_check(constraint.expression, field_types)
        except CELValidationError as exc:
            issues.append(SchemaValidationIssue(f"constraints[{i}].expression", str(exc)))

    fsm_issues = validate_state_machines(field_types, definition.state_machines)
    issues.extend(SchemaValidationIssue(i.field_path, i.message) for i in fsm_issues)

    tag_issues = validate_tags(definition)
    issues.extend(SchemaValidationIssue(i.field_path, i.message) for i in tag_issues)

    all_field_keys = {f.key for f in definition.fields}
    view_issues = validate_views(all_field_keys, definition.views)
    issues.extend(SchemaValidationIssue(i.field_path, i.message) for i in view_issues)

    return issues


def _validate_field_value(field: FieldDef, value: object) -> None:
    if field.type == "string" and not isinstance(value, str):
        raise FieldValidationError(f"field {field.key!r}: expected string, got {value!r}")
    if field.type == "boolean" and not isinstance(value, bool):
        raise FieldValidationError(f"field {field.key!r}: expected boolean, got {value!r}")
    if field.type in ("integer", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FieldValidationError(f"field {field.key!r}: expected {field.type}, got {value!r}")
        if field.type == "integer" and not float(value).is_integer():
            raise FieldValidationError(f"field {field.key!r}: expected integer, got {value!r}")
        if field.minimum is not None and value < field.minimum:
            raise FieldValidationError(
                f"field {field.key!r}: {value} is below minimum {field.minimum}"
            )
        if field.maximum is not None and value > field.maximum:
            raise FieldValidationError(
                f"field {field.key!r}: {value} is above maximum {field.maximum}"
            )
    if field.type == "array" and not isinstance(value, list):
        raise FieldValidationError(f"field {field.key!r}: expected array, got {value!r}")
    if field.enum is not None:
        allowed = value if field.type != "array" else None
        candidates = value if field.type == "array" and isinstance(value, list) else [allowed]
        for candidate in candidates:
            if candidate not in field.enum:
                raise FieldValidationError(
                    f"field {field.key!r}: {candidate!r} is not one of {field.enum}"
                )


def validate_fields(definition: EntitySchemaDefinition, data: Mapping[str, object]) -> None:
    fields_by_key = {f.key: f for f in definition.fields}
    unknown = set(data) - set(fields_by_key)
    if unknown:
        raise FieldValidationError(f"unknown field(s): {sorted(unknown)}")
    for key, field in fields_by_key.items():
        if key in data:
            _validate_field_value(field, data[key])


def compute_derived(
    definition: EntitySchemaDefinition, data: Mapping[str, object]
) -> dict[str, object]:
    """Never persisted -- recomputed fresh from ``data`` every time it's needed (write
    validation, constraint checks, rendering)."""
    computed: dict[str, object] = {}
    activation_fields = dict(data)
    for derived in definition.derived:
        raw = evaluate(derived.expression, activation_fields)
        typed = COERCE_BY_TYPE[derived.type](raw)
        computed[derived.key] = typed
        activation_fields[derived.key] = typed
    return computed


def check_constraints(
    definition: EntitySchemaDefinition,
    data: Mapping[str, object],
    derived: Mapping[str, object],
) -> None:
    combined = {**data, **derived}
    for constraint in definition.constraints:
        result = evaluate(constraint.expression, combined)
        if not bool(result):
            raise ConstraintViolationError(constraint.expression, constraint.message)


def _loc_to_path(loc: tuple[object, ...]) -> str:
    parts: list[str] = []
    for part in loc:
        if isinstance(part, int):
            parts[-1] = f"{parts[-1]}[{part}]"
        else:
            parts.append(str(part))
    return ".".join(parts)


def validate_raw(
    raw: dict[str, object],
) -> tuple[EntitySchemaDefinition | None, list[SchemaValidationIssue]]:
    """The dry-run entrypoint (mirrors ``core.process.dsl.validator.validate_raw``
    exactly): structural (Pydantic) validation first -- a document that fails it (bad
    shape, unknown field type, malformed range) never reaches the semantic pass below,
    which assumes a structurally valid document to walk. A caller (the schema editor's
    live-validate-on-keystroke call) never needs to know which layer caught a given
    problem, only the field-anchored issue list."""
    try:
        definition = EntitySchemaDefinition.model_validate(raw)
    except pydantic.ValidationError as exc:
        return None, [
            SchemaValidationIssue(field_path=_loc_to_path(error["loc"]), message=error["msg"])
            for error in exc.errors()
        ]
    return definition, validate_schema_definition(definition)


def validate_and_prepare_write(
    definition: EntitySchemaDefinition, data: Mapping[str, object]
) -> Mapping[str, object]:
    """The full write-time pass: field validation -> derived computation (transient, for
    the constraint check only) -> constraint check. Returns ``data`` unchanged (derived
    values are never part of what's persisted) once every check passes."""
    validate_fields(definition, data)
    derived = compute_derived(definition, data)
    check_constraints(definition, data, derived)
    return data
