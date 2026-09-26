"""Semantic tag system: how a generic engine renders an HP bar
without knowing what HP is. Fields carry tags from this **fixed, closed** vocabulary,
and a tag->widget registry (data, not a switch statement) maps them to render
behaviour. Tags, not field names, drive rendering -- ``hit_points [resource]``,
``budget_remaining [resource]``, and a work item's remaining estimate all resolve to
the exact same widget, differing only in metadata values.

**A closed set on purpose** (design decision): an open vocabulary quietly
becomes field-name semantics again, and the renderer grows a switch per pack -- exactly
what the no-user-code rule forbids. These nine tags cover every schema across all three
overlays; a tenth is a deliberate *core* decision to propose, never pack
data (INV-9's all-packs smoke test is what keeps everyone honest about this).
"""

from __future__ import annotations

from dataclasses import dataclass

from core.entities.cel import CELValidationError, compile_check
from core.entities.schema import EntitySchemaDefinition, FieldDef

SEMANTIC_TAGS: frozenset[str] = frozenset(
    {
        "resource",
        "attribute",
        "status_set",
        "progression",
        "identity",
        "descriptor",
        "relationship",
        "modifier_source",
        "private",
    }
)

# tag -> widget id. Total (every core tag maps to something, test_tag_widget_registry_
# is_total_and_domain_blind asserts this) and domain-blind (no reference to any pack or
# domain content anywhere in this module) -- the React components live in the frontend;
# this is the registry *contract* those components are keyed by.
TAG_WIDGET_REGISTRY: dict[str, str] = {
    "resource": "resource_bar",
    "attribute": "attribute_block",
    "status_set": "status_chip_row",
    "progression": "progression_meter",
    "identity": "identity_text",
    "descriptor": "descriptor_text",
    "relationship": "relationship_link",
    # Co-occurs with `attribute` on the same field (a modifier-bearing attribute); same
    # display block, the modifier is one more piece of that block's rendered data.
    "modifier_source": "attribute_block",
    # Not itself a rendered widget -- a visibility marker other tags compose with
    # (a `private` `resource` is still a resource_bar, just scope-filtered upstream).
    "private": "private_marker",
}


def widget_for(tag: str) -> str:
    return TAG_WIDGET_REGISTRY[tag]


@dataclass(frozen=True)
class TagValidationIssue:
    field_path: str
    message: str


def _validate_field_tags(
    field_path: str, field: FieldDef, all_field_keys: set[str], field_types: dict[str, str]
) -> list[TagValidationIssue]:
    issues: list[TagValidationIssue] = []

    unknown = sorted(set(field.tags) - SEMANTIC_TAGS)
    for tag in unknown:
        issues.append(
            TagValidationIssue(field_path, f"unknown semantic tag {tag!r} on field {field.key!r}")
        )
    if unknown:
        # An unknown tag makes the rest of this field's tag-metadata contracts
        # unevaluable in any meaningful way -- report it alone, matching the FSM
        # validator's "one bad thing per field at a time" ordering.
        return issues

    tags = set(field.tags)

    if "resource" in tags:
        max_ref = field.tag_metadata.get("max_ref")
        if not isinstance(max_ref, str) or max_ref not in all_field_keys:
            issues.append(
                TagValidationIssue(
                    field_path,
                    f"field {field.key!r}: 'resource' tag requires tag_metadata.max_ref "
                    f"naming a declared field, got {max_ref!r}",
                )
            )
        if not isinstance(field.tag_metadata.get("low_threshold"), (int, float)):
            issues.append(
                TagValidationIssue(
                    field_path,
                    f"field {field.key!r}: 'resource' tag requires a numeric "
                    "tag_metadata.low_threshold",
                )
            )

    if "modifier_source" in tags:
        formula = field.tag_metadata.get("modifier_formula")
        if not isinstance(formula, str) or not formula:
            issues.append(
                TagValidationIssue(
                    field_path,
                    f"field {field.key!r}: 'modifier_source' tag requires tag_metadata"
                    ".modifier_formula (CEL)",
                )
            )
        elif isinstance(formula, str):
            try:
                compile_check(formula, field_types)
            except CELValidationError as exc:
                issues.append(
                    TagValidationIssue(f"{field_path}.tag_metadata.modifier_formula", str(exc))
                )

    if "progression" in tags and not isinstance(field.tag_metadata.get("curve_ref"), str):
        issues.append(
            TagValidationIssue(
                field_path,
                f"field {field.key!r}: 'progression' tag requires tag_metadata.curve_ref",
            )
        )

    if "status_set" in tags and field.type != "array":
        issues.append(
            TagValidationIssue(
                field_path, f"field {field.key!r}: 'status_set' tag requires an array-typed field"
            )
        )

    if "private" in tags and field.scope_key is None:
        issues.append(
            TagValidationIssue(
                field_path, f"field {field.key!r}: 'private' tag requires a scope_key"
            )
        )

    return issues


def validate_tags(definition: EntitySchemaDefinition) -> list[TagValidationIssue]:
    all_keys = {f.key for f in definition.fields}
    field_types = definition.field_types()
    issues: list[TagValidationIssue] = []
    for i, field in enumerate(definition.fields):
        issues.extend(_validate_field_tags(f"fields[{i}]", field, all_keys, field_types))
    return issues
