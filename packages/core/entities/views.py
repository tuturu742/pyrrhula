"""ViewDef: layout (grouping, ordering, tabs) referencing fields by
key. Making a sheet *feel* native is a ``ViewDef`` **content** problem -- pack authoring,
F3.7/F3.8/its own job -- not an engine problem: the engine only resolves field keys
to widgets (``core.entities.tags``) and lays out groups/tabs in the declared order.
the React components read a ``ViewDef`` at render time; a field the ``ViewDef``
doesn't mention still renders (the "falls into a default group, never disappears"),
so a dangling field reference here is a authoring mistake to flag, never a silent drop.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict


class ViewGroupDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label_key: str
    field_keys: list[str] = []


class ViewTabDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label_key: str
    group_keys: list[str] = []


class ViewDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    groups: list[ViewGroupDef] = []
    tabs: list[ViewTabDef] = []


@dataclass(frozen=True)
class ViewValidationIssue:
    field_path: str
    message: str


def validate_views(all_field_keys: set[str], views: list[ViewDef]) -> list[ViewValidationIssue]:
    issues: list[ViewValidationIssue] = []
    for v_idx, view in enumerate(views):
        path_prefix = f"views[{v_idx}]"
        group_keys = {g.key for g in view.groups}

        for g_idx, group in enumerate(view.groups):
            for field_key in group.field_keys:
                if field_key not in all_field_keys:
                    issues.append(
                        ViewValidationIssue(
                            f"{path_prefix}.groups[{g_idx}].field_keys",
                            f"view {view.key!r} group {group.key!r} references undeclared "
                            f"field {field_key!r}",
                        )
                    )

        for t_idx, tab in enumerate(view.tabs):
            for group_key in tab.group_keys:
                if group_key not in group_keys:
                    issues.append(
                        ViewValidationIssue(
                            f"{path_prefix}.tabs[{t_idx}].group_keys",
                            f"view {view.key!r} tab {tab.key!r} references undeclared "
                            f"group {group_key!r}",
                        )
                    )
    return issues
