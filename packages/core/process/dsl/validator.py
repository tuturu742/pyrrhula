"""Static validation for a ProcessDefinition (B1.1, plan §5.2): everything Pydantic's
schema-level constraints (schema.py -- required ``visibility``, known agent roles, exactly-
one-selector shapes, valid durations) can't check because it requires looking at the
*document as a whole* rather than one field at a time: transition-graph integrity, budget
ratio sums, and CEL expression validity against the document's own declared state.

``validate_raw`` is the single entrypoint the API/authoring layer calls: it does both
Pydantic's structural pass and this module's graph/semantic pass, normalising both failure
modes into the same field-addressed ``ValidationIssue`` shape so a caller (D1.2's editor)
never needs to know which layer caught a given problem.

**"No loops except declared ones" (plan §5.2) is not a separate rule enforced here.** The
DSL has no implicit iteration construct (no ``while``/``for``) -- the *only* way a cycle
can exist in the transition graph is if an author's own ``gates``/``on_complete``/
``await.on_timeout`` explicitly points back at an earlier phase, which is precisely what
"declared" means. The plan's own §5.2 example is itself cyclic (``open_discussion`` <->
``action_phase`` <-> ``resolution``, and ``resolution`` -> ``feedback_loop`` ->
``open_discussion``) and must validate cleanly -- so "reject cycles" would be a bug, not a
missing feature. What *is* checked (dangling-transition + reachability, below) already
guarantees every edge in a cycle points at a real, reachable phase; there is nothing more
"declared-ness" could mean structurally.
"""

from __future__ import annotations

from dataclasses import dataclass

import pydantic

from core.process.dsl.cel import CELValidationError, compile_check
from core.process.dsl.schema import PhaseSpec, ProcessDefinitionDSL

_BUDGET_RATIO_TOLERANCE = 0.02


@dataclass(frozen=True)
class ValidationIssue:
    field_path: str
    message: str


def validate_raw(
    raw: dict[str, object],
) -> tuple[ProcessDefinitionDSL | None, list[ValidationIssue]]:
    """Structural (Pydantic) pass first -- a document that fails it (bad shape, missing
    visibility, unknown role, malformed duration, ...) never reaches the graph/semantic
    checks below, which assume a structurally valid document to walk."""
    try:
        dsl = ProcessDefinitionDSL.model_validate(raw)
    except pydantic.ValidationError as exc:
        return None, [
            ValidationIssue(field_path=_loc_to_path(error["loc"]), message=error["msg"])
            for error in exc.errors()
        ]
    return dsl, validate_definition(dsl)


def _loc_to_path(loc: tuple[object, ...]) -> str:
    parts: list[str] = []
    for part in loc:
        if isinstance(part, int):
            parts[-1] = f"{parts[-1]}[{part}]"
        else:
            parts.append(str(part))
    return ".".join(parts)


def validate_definition(dsl: ProcessDefinitionDSL) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues.extend(_check_initial_phase(dsl))
    issues.extend(_check_transition_targets(dsl))
    issues.extend(_check_transition_mechanism(dsl))
    issues.extend(_check_gate_else_is_last(dsl))
    issues.extend(_check_budget_ratios(dsl))
    issues.extend(_check_cel_expressions(dsl))
    issues.extend(_check_phase_requirements(dsl))
    # Reachability assumes every referenced target is real -- run it last, and skip it
    # entirely if dangling targets already exist, so one bad edge doesn't also spam
    # "unreachable" for everything downstream of it.
    if not any(i.field_path.startswith("phases.") and "to" in i.field_path for i in issues):
        issues.extend(_check_reachability(dsl))
    return issues


def _check_phase_requirements(dsl: ProcessDefinitionDSL) -> list[ValidationIssue]:
    """A ``requires`` block that can never be satisfied, or never fires, caught here.

    Two ways to write one that looks like a rule and is not: a phase whose actors can
    produce nothing (no ``generate`` entry) but which demands output, and a declared
    block whose every count is zero. The first stalls or repeats pointlessly; the second
    reads as a requirement to anyone maintaining the flow and enforces nothing.
    """
    issues: list[ValidationIssue] = []
    for phase_key, phase in dsl.phases.items():
        spec = phase.requires
        if spec is None:
            continue
        path = f"phases.{phase_key}.requires"
        if not spec.is_declared():
            issues.append(
                ValidationIssue(
                    path,
                    "every count is zero: the block requires nothing. Remove it, or say "
                    "what the phase has to produce.",
                )
            )
            continue
        if not any(a.mode in ("generate", "generate_as") for a in phase.actors):
            issues.append(
                ValidationIssue(
                    path,
                    f"phase {phase_key!r} has no generating actor, so it cannot produce "
                    "what this asks of it: the requirement can only repeat or hold.",
                )
            )
        if spec.on_unmet == "hold" and spec.max_repeats == 0 and phase.await_field is not None:
            issues.append(
                ValidationIssue(
                    path,
                    "on_unmet 'hold' on a phase that already awaits: the phase would "
                    "park for two different reasons and the second is unreachable.",
                )
            )
    return issues


def _check_initial_phase(dsl: ProcessDefinitionDSL) -> list[ValidationIssue]:
    if dsl.initial_phase not in dsl.phases:
        return [
            ValidationIssue(
                "initial_phase",
                f"initial_phase {dsl.initial_phase!r} is not a declared phase",
            )
        ]
    return []


def _transition_targets(phase_key: str, phase: PhaseSpec) -> list[tuple[str, str]]:
    """(field_path, target_phase_key) for every transition this phase can take."""
    targets: list[tuple[str, str]] = []
    if phase.on_complete is not None:
        targets.append((f"phases.{phase_key}.on_complete", phase.on_complete))
    for i, gate in enumerate(phase.gates):
        targets.append((f"phases.{phase_key}.gates[{i}].to", gate.to))
    if phase.await_field is not None:
        targets.append((f"phases.{phase_key}.await.on_timeout", phase.await_field.on_timeout))
    return targets


def _check_transition_targets(dsl: ProcessDefinitionDSL) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for phase_key, phase in dsl.phases.items():
        for field_path, target in _transition_targets(phase_key, phase):
            if target not in dsl.phases:
                issues.append(
                    ValidationIssue(
                        field_path, f"transition target {target!r} is not a declared phase"
                    )
                )
    return issues


def _check_transition_mechanism(dsl: ProcessDefinitionDSL) -> list[ValidationIssue]:
    """One phase, one transition mechanism once actors are exhausted: gates and
    on_complete are alternatives, not layers -- having both set is an unresolvable
    ambiguity about which one the interpreter should honour, not a feature."""
    issues: list[ValidationIssue] = []
    for phase_key, phase in dsl.phases.items():
        if phase.gates and phase.on_complete is not None:
            issues.append(
                ValidationIssue(
                    f"phases.{phase_key}",
                    "a phase may declare gates or on_complete, not both",
                )
            )
    return issues


def _check_gate_else_is_last(dsl: ProcessDefinitionDSL) -> list[ValidationIssue]:
    """Gates evaluate in declaration order, first match wins -- an 'else' gate anywhere
    but last makes every gate after it structurally unreachable."""
    issues: list[ValidationIssue] = []
    for phase_key, phase in dsl.phases.items():
        for i, gate in enumerate(phase.gates):
            if gate.else_ and i != len(phase.gates) - 1:
                issues.append(
                    ValidationIssue(
                        f"phases.{phase_key}.gates[{i}]",
                        "an 'else' gate must be the last gate in the list",
                    )
                )
    return issues


def _check_budget_ratios(dsl: ProcessDefinitionDSL) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for phase_key, phase in dsl.phases.items():
        if phase.budget is None:
            continue
        total = sum(phase.budget.ratio.values())
        if abs(total - 1.0) > _BUDGET_RATIO_TOLERANCE:
            issues.append(
                ValidationIssue(
                    f"phases.{phase_key}.budget.ratio",
                    f"budget ratios sum to {total!r}, expected ~1.0 (+/-{_BUDGET_RATIO_TOLERANCE})",
                )
            )
    return issues


def _check_cel_expressions(dsl: ProcessDefinitionDSL) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for phase_key, phase in dsl.phases.items():
        for i, gate in enumerate(phase.gates):
            if gate.when is not None:
                try:
                    compile_check(gate.when, dsl.state)
                except CELValidationError as exc:
                    issues.append(ValidationIssue(f"phases.{phase_key}.gates[{i}].when", str(exc)))
        for i, effect in enumerate(phase.effects):
            try:
                compile_check(effect.to, dsl.state)
            except CELValidationError as exc:
                issues.append(ValidationIssue(f"phases.{phase_key}.effects[{i}].to", str(exc)))
    return issues


def _check_reachability(dsl: ProcessDefinitionDSL) -> list[ValidationIssue]:
    if dsl.initial_phase not in dsl.phases:
        return []  # already reported by _check_initial_phase

    reachable: set[str] = set()
    frontier = [dsl.initial_phase]
    while frontier:
        current = frontier.pop()
        if current in reachable:
            continue
        reachable.add(current)
        phase = dsl.phases[current]
        for _field_path, target in _transition_targets(current, phase):
            if target in dsl.phases and target not in reachable:
                frontier.append(target)

    unreachable = set(dsl.phases) - reachable
    return [
        ValidationIssue(f"phases.{key}", "phase is unreachable from initial_phase")
        for key in sorted(unreachable)
    ]
