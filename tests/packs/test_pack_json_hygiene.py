"""Two ways a pack's JSON can be wrong while still parsing.

Neither is caught by loading the pack: `json.loads` keeps the last of a duplicated key
and silently drops the first, and a view group may name a field key the schema never
declares. Both were real -- an overlay label added under a key that already existed
further down the file had no effect at all, and the character sheet's view groups named
six ability scores and omitted two declared fields, so the sheet rendered without them.

Static, no database: this reads the shipped pack files and nothing else.
"""

from __future__ import annotations

import json
import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PACK_ROOTS = (_REPO_ROOT / "builtin-workflows", _REPO_ROOT / ".plugins" / "default")


def _pack_json_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in _PACK_ROOTS:
        if root.is_dir():
            files.extend(sorted(root.rglob("*.json")))
    return files


_JSON_FILES = _pack_json_files()


def _duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen.add(key)
    return dict(pairs)


@pytest.mark.parametrize("path", _JSON_FILES, ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_no_object_in_a_pack_file_declares_the_same_key_twice(path: pathlib.Path) -> None:
    try:
        json.loads(path.read_text(), object_pairs_hook=_duplicate_keys)
    except ValueError as exc:
        pytest.fail(f"{path.relative_to(_REPO_ROOT)}: {exc}")


@pytest.mark.parametrize(
    "path",
    [p for p in _JSON_FILES if p.parent.name == "schemas"],
    ids=lambda p: str(p.relative_to(_REPO_ROOT)),
)
def test_every_declared_field_appears_in_some_view_group(path: pathlib.Path) -> None:
    """A field the schema declares and no view group names is invisible on the sheet --
    stored, validated, mutable by tools, and never shown to the person reading it."""
    payload = json.loads(path.read_text())
    definition = payload.get("definition", payload)
    declared = {field["key"] for field in definition.get("fields", [])}
    if not declared:
        pytest.skip("no fields declared")
    views = definition.get("views", [])
    if not views:
        pytest.skip("no views declared; nothing claims to render this schema")

    grouped: set[str] = set()
    for view in views:
        for group in view.get("groups", []):
            grouped.update(group.get("field_keys", []))

    unknown = grouped - declared
    assert not unknown, f"view groups name fields the schema does not declare: {sorted(unknown)}"

    missing = declared - grouped
    assert not missing, f"declared fields no view group renders: {sorted(missing)}"
