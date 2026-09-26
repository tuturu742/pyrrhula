"""`prompt_directive` binding: banded rendering. Models follow "you speak
rarely, and only when you have something substantive to add" far more reliably than
"chattiness: 20/100" -- band the value, never inject the raw number. Band boundaries
and template text are pack data (an axis definition's `bindings` JSONB), never code:
"you wave things through" (swdev `review_strictness`) and "you speak in riddles" (RPG
oracle) are content decisions, and packs may not require core changes to express them
(INV-9).

Deliberately operates on the raw `bindings` JSONB shape (`list[dict[str, object]]`), not
`core.behavior.validation`'s Pydantic schema -- keeps this module decoupled from that
one's import graph; both independently agree on what a `prompt_directive` binding's
`bands` field looks like.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from core.behavior.models import AxisDefinitionRow


class DirectiveBindingError(Exception):
    pass


class BandSchema(BaseModel):
    """Inclusive on both ends (`min <= value <= max`); `validate_bands` requires bands
    to tile `[range_min, range_max]` with no gap and no overlap -- "total" per this
    task's own acceptance criterion, so no legal axis value ever renders nothing."""

    model_config = ConfigDict(extra="forbid")

    min: int
    max: int
    text: str


def _prompt_directive_bindings(bindings: list[dict[str, object]]) -> list[dict[str, object]]:
    return [b for b in bindings if b.get("kind") == "prompt_directive"]


def _bands_from_binding(binding: dict[str, object]) -> list[BandSchema]:
    raw_bands = binding.get("bands", [])
    if not isinstance(raw_bands, list):
        raise DirectiveBindingError("prompt_directive binding's 'bands' must be a list")
    return [BandSchema.model_validate(b) for b in raw_bands]


def validate_bands(axis_key: str, range_min: int, range_max: int, bands: list[BandSchema]) -> None:
    if not bands:
        raise DirectiveBindingError(
            f"axis {axis_key!r}: prompt_directive binding declares no bands"
        )
    ordered = sorted(bands, key=lambda b: b.min)
    if ordered[0].min != range_min:
        raise DirectiveBindingError(
            f"axis {axis_key!r}: bands must start at range_min ({range_min}), "
            f"first band starts at {ordered[0].min}"
        )
    if ordered[-1].max != range_max:
        raise DirectiveBindingError(
            f"axis {axis_key!r}: bands must end at range_max ({range_max}), "
            f"last band ends at {ordered[-1].max}"
        )
    for prev, cur in zip(ordered, ordered[1:], strict=False):
        if prev.max >= cur.min:
            raise DirectiveBindingError(
                f"axis {axis_key!r}: bands {prev.min}-{prev.max} and {cur.min}-{cur.max} overlap"
            )
        if cur.min != prev.max + 1:
            raise DirectiveBindingError(
                f"axis {axis_key!r}: gap between bands {prev.min}-{prev.max} and "
                f"{cur.min}-{cur.max} -- no value in between maps to any band"
            )


def validate_prompt_directive_bindings(
    key: str, range_min: int, range_max: int, bindings: list[dict[str, object]]
) -> None:
    """Called at pack-load time (`core.behavior.repo.create_axis_definition`), alongside
    `core.behavior.validation.validate_axis_definition` -- a separate function, not
    folded into that one, to keep this module's own import graph independent (see
    module docstring). Takes primitives, not an `AxisDefinitionRow`, so it can validate
    a definition *before* any row exists (pack-load time is exactly that moment)."""
    for binding in _prompt_directive_bindings(bindings):
        bands = _bands_from_binding(binding)
        validate_bands(key, range_min, range_max, bands)


def render_directive(axis: AxisDefinitionRow, value: int) -> str | None:
    """`None` if the axis carries no `prompt_directive` binding at all -- not every axis
    does (a stakes:high axis may rely solely on its gate binding, e.g.
    `deception_propensity`). Bands are re-validated here, cheaply, rather than trusting
    pack-load-time validation was actually run for this row -- the render path is where
    a coverage gap would otherwise surface as a silently-missing directive, not a loud
    error."""
    directives: list[str] = []
    for binding in _prompt_directive_bindings(axis.bindings):
        bands = _bands_from_binding(binding)
        validate_bands(axis.key, axis.range_min, axis.range_max, bands)
        for band in bands:
            if band.min <= value <= band.max:
                directives.append(band.text)
                break
        else:  # pragma: no cover -- unreachable once validate_bands has passed
            raise DirectiveBindingError(f"axis {axis.key!r}: value {value} matches no band")
    if not directives:
        return None
    return " ".join(directives)


def render_directives_for_profile(
    axis_definitions: list[AxisDefinitionRow], axis_values: dict[str, int]
) -> str:
    """The one call site (`core.process.live_session`) uses: render every axis in the
    active profile that carries a `prompt_directive` binding, joined into the block that
    enters the layout's *stable* region -- unchanging within a session unless the
    profile version itself changes, preserving the prompt-cache prefix."""
    rendered: list[str] = []
    for axis in axis_definitions:
        value = axis_values.get(axis.key)
        if value is None:
            continue
        text = render_directive(axis, value)
        if text:
            rendered.append(text)
    return " ".join(rendered)
