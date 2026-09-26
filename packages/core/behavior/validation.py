"""Axis definition validation: the load-bearing rule is
`stakes: high ⇒ a gate binding is present`, checked at authoring/load time so a
prompt-only malice slider can never be authored at all -- 97% per-turn prompt adherence
over 60 relevant turns is an ~84% cumulative leak probability; for a high-stakes axis,
prompt injection is not a control, it is a delay.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# The binding-kind taxonomy: an axis may carry several. `sampling` is documented
# as weak by design -- it nudges temperature/top_p, never a control claim, so it's a
# legal binding for a stakes:high axis to carry *in addition to* a gate, but never on
# its own.
BINDING_KINDS = frozenset({"prompt_directive", "gate", "retrieval_bias", "sampling", "tool_policy"})

_VALID_STAKES = ("low", "high")


class AxisValidationError(Exception):
    pass


class BindingSchema(BaseModel):
    """`kind` is the only field core validation cares about; everything else is
    kind-specific (a `gate` binding's `gate_id`, a `retrieval_bias`'s weight curve, ...)
    and deliberately opaque here -- `extra="allow"` lets each binding kind's own
    consumer (the prompt_directive binder, the gate) define its own shape without
    this schema needing to know it."""

    model_config = ConfigDict(extra="allow")

    kind: str


class AxisDefinitionSchema(BaseModel):
    """Validated authoring/pack-load-time shape, checked by `validate_axis_definition`
    before an `AxisDefinitionRow` is written -- mirrors `RuleSystemDefinitionSchema`'s
    identical split."""

    model_config = ConfigDict(extra="forbid")

    pack_id: str
    key: str
    label_key: str
    range_min: int = 0
    range_max: int = 100
    default: int | None = None
    stakes: str
    semantics_md: str
    bindings: list[BindingSchema] = []


def validate_axis_definition(definition: AxisDefinitionSchema) -> None:
    if definition.stakes not in _VALID_STAKES:
        raise AxisValidationError(
            f"axis {definition.key!r}: stakes must be one of {_VALID_STAKES}, "
            f"got {definition.stakes!r}"
        )
    if definition.range_min >= definition.range_max:
        raise AxisValidationError(
            f"axis {definition.key!r}: range_min ({definition.range_min}) must be less "
            f"than range_max ({definition.range_max})"
        )
    if definition.default is not None and not (
        definition.range_min <= definition.default <= definition.range_max
    ):
        raise AxisValidationError(
            f"axis {definition.key!r}: default ({definition.default}) is outside "
            f"[{definition.range_min}, {definition.range_max}]"
        )

    binding_kinds = {b.kind for b in definition.bindings}
    unknown_kinds = binding_kinds - BINDING_KINDS
    if unknown_kinds:
        raise AxisValidationError(
            f"axis {definition.key!r}: unknown binding kind(s) {sorted(unknown_kinds)}, "
            f"must be one of {sorted(BINDING_KINDS)}"
        )

    if definition.stakes == "high" and "gate" not in binding_kinds:
        raise AxisValidationError(
            f"axis {definition.key!r} is stakes:high but declares no gate binding -- a "
            "high-stakes axis must be enforced by a gate, not prompt adherence alone (§8.3)"
        )
