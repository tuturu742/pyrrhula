"""Rule-system validation (C1.5, plan §9.2 step 2/§9.3): the API that closes the "I have
+5" hallucination. A model emits a REQUEST (an expression + a check type + which entity it
claims to be); this module -- not the model -- decides whether that request is legal and
what the actual modifier is, reading it from ``actor_fields`` rather than trusting
whatever the expression's own literal modifier says.

The same code path (``validate()``) accepts any ``RuleSystemDefinition`` -- a d20 system,
a PbtA-style banded system, a coin flip -- with no branching on which one it is. That's
the real test this task exists to pass (mirrors INV-9's pack-independence property one
level down, at the rule-system level rather than the whole-pack level).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import celpy
from celpy.adapter import json_to_cel

from core.resolution.grammar import GrammarError, ModifierTerm, ParsedExpression, parse_expression
from core.resolution.rule_system import RuleSystemDefinition

_cel_env = celpy.Environment()


@dataclass(frozen=True)
class ValidationError:
    # 'illegal_expression' | 'unknown_check_type' | 'check_not_legal_in_phase' |
    # 'modifier_resolution_failed' | 'modifier_mismatch' | 'validator_failed'
    code: str
    message: str
    expected_modifier: int | None = None


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    parsed: ParsedExpression | None = None
    computed_modifier: int | None = None
    error: ValidationError | None = None


def _eval_cel(expression: str, namespace: dict[str, Any]) -> Any:
    ast = _cel_env.compile(expression)
    program = _cel_env.program(ast)
    activation = json_to_cel(namespace)
    return program.evaluate(activation)  # type: ignore[arg-type]


def _claimed_modifier(modifiers: tuple[ModifierTerm, ...]) -> int | None:
    """The model's claimed numeric modifier, if the expression carried any literal
    (non-symbolic) terms -- ``None`` if every term was symbolic (``2d6+STR``), meaning
    there's nothing literal to compare against; the computed modifier is still
    authoritative either way, it just isn't being cross-checked against a claim here."""
    total = 0
    found_literal = False
    for m in modifiers:
        if m.value is None:
            continue
        found_literal = True
        total += m.value if m.sign == "+" else -m.value
    return total if found_literal else None


def validate(
    expression: str,
    check_type: str,
    actor_fields: dict[str, object],
    rule_system: RuleSystemDefinition,
    legal_check_types: frozenset[str] | None,
) -> ValidationResult:
    """``legal_check_types``: the check types the *current phase* permits (e.g. derived
    from ``PhaseSpec.tools``) -- ``None`` means "no phase restriction applies" (a caller
    validating outside a running session, e.g. authoring-time testing)."""
    try:
        parsed = parse_expression(expression)
    except GrammarError as exc:
        return ValidationResult(ok=False, error=ValidationError("illegal_expression", str(exc)))

    allowed_sides = rule_system.dice_grammar.get("allowed_sides")
    if allowed_sides is not None and parsed.sides not in allowed_sides:  # type: ignore[operator]
        return ValidationResult(
            ok=False,
            parsed=parsed,
            error=ValidationError(
                "illegal_expression",
                f"{parsed.sides}-sided dice are not legal in {rule_system.key!r}",
            ),
        )

    max_dice = rule_system.dice_grammar.get("max_dice_count")
    if max_dice is not None and parsed.count > max_dice:  # type: ignore[operator]
        return ValidationResult(
            ok=False,
            parsed=parsed,
            error=ValidationError(
                "illegal_expression", f"{parsed.count} dice exceeds the {max_dice} maximum"
            ),
        )

    if parsed.keep is not None and not rule_system.dice_grammar.get("allow_keep_drop", False):
        return ValidationResult(
            ok=False,
            parsed=parsed,
            error=ValidationError(
                "illegal_expression", "keep/drop syntax is not legal in this rule system"
            ),
        )

    if check_type not in rule_system.check_types:
        return ValidationResult(
            ok=False,
            parsed=parsed,
            error=ValidationError(
                "unknown_check_type",
                f"{check_type!r} is not a check type in {rule_system.key!r} "
                f"(legal: {sorted(rule_system.check_types)})",
            ),
        )

    if legal_check_types is not None and check_type not in legal_check_types:
        return ValidationResult(
            ok=False,
            parsed=parsed,
            error=ValidationError(
                "check_not_legal_in_phase", f"{check_type!r} is not legal in this phase"
            ),
        )

    resolver_expr = rule_system.modifier_resolver.get(check_type)
    if resolver_expr is None:
        return ValidationResult(
            ok=False,
            parsed=parsed,
            error=ValidationError(
                "unknown_check_type", f"no modifier_resolver defined for {check_type!r}"
            ),
        )

    try:
        computed_modifier = int(_eval_cel(resolver_expr, {"fields": actor_fields}))
    except Exception as exc:  # noqa: BLE001 -- any CEL failure here is a validation failure
        return ValidationResult(
            ok=False,
            parsed=parsed,
            error=ValidationError("modifier_resolution_failed", f"modifier_resolver failed: {exc}"),
        )

    claimed = _claimed_modifier(parsed.modifiers)
    if claimed is not None and claimed != computed_modifier:
        return ValidationResult(
            ok=False,
            parsed=parsed,
            computed_modifier=computed_modifier,
            error=ValidationError(
                "modifier_mismatch",
                f"claimed modifier {claimed:+d} does not match the actor's actual "
                f"{computed_modifier:+d} for {check_type!r}",
                expected_modifier=computed_modifier,
            ),
        )

    for predicate in rule_system.validators:
        try:
            passed = bool(
                _eval_cel(
                    predicate,
                    {"fields": actor_fields, "expression": expression, "check_type": check_type},
                )
            )
        except Exception as exc:  # noqa: BLE001 -- same: a validation failure, not a crash
            return ValidationResult(
                ok=False,
                parsed=parsed,
                computed_modifier=computed_modifier,
                error=ValidationError(
                    "validator_failed", f"validator {predicate!r} errored: {exc}"
                ),
            )
        if not passed:
            return ValidationResult(
                ok=False,
                parsed=parsed,
                computed_modifier=computed_modifier,
                error=ValidationError(
                    "validator_failed", f"validator {predicate!r} rejected the expression"
                ),
            )

    return ValidationResult(ok=True, parsed=parsed, computed_modifier=computed_modifier)
