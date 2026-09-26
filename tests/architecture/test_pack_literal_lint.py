"""the core-side half of the zero-core-diff assertion (INV-9): no module
under ``packages/core/`` may reference a pack identifier (``rpg``, ``enterprise``,
``swdev``) as a string literal -- the generic engine must never know which pack it's
running. Complements ``test_packs_independence.py`` (the other direction: packs may
not import core).

Uses the AST, not a substring scan, and only flags a literal used as *code* (a
conditional, a dict key, an argument) -- not prose mentioning the word inside a
docstring, which several modules legitimately do when explaining this exact rule (this
file's own docstring included). A bare substring scan would flag its own explanation.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"

_PACK_LITERALS = frozenset({"rpg", "enterprise", "swdev"})


def _docstring_constant_ids(tree: ast.Module) -> set[int]:
    """``id()`` of every string-constant node that's a real docstring (the first
    statement of the module or of any function/class body, wrapped in an ``Expr``) --
    excluded from the literal scan the same way a docstring is exempt from every other
    "no hardcoded X" lint in this repo (e.g. ``test_vocabulary_lint.py``)."""
    docstring_ids: set[int] = set()
    candidates: list[ast.AST] = [tree]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            candidates.append(node)
    for owner in candidates:
        body = getattr(owner, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            docstring_ids.add(id(body[0].value))
    return docstring_ids


def find_pack_literals(path: pathlib.Path) -> list[str]:
    """Returns the pack literals this file references as code, if any. Public so the
    self-test below can prove the scanner actually fires against a planted file."""
    tree = ast.parse(path.read_text(), filename=str(path))
    docstring_ids = _docstring_constant_ids(tree)

    hits: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _PACK_LITERALS
            and id(node) not in docstring_ids
        ):
            hits.append(node.value)
    return hits


def test_core_referencing_a_pack_literal_fails_lint() -> None:
    offenders: list[str] = []
    for path in PACKAGES.rglob("*.py"):
        if "tests" in path.parts:
            continue
        hits = find_pack_literals(path)
        if hits:
            offenders.append(f"{path.relative_to(ROOT)}: {sorted(set(hits))}")

    assert not offenders, (
        "packages/core/ must never reference a pack identifier as a literal (INV-9) -- "
        "the generic engine cannot know which pack is running. Offending files:\n"
        + "\n".join(offenders)
    )


def test_the_scan_actually_finds_something_when_present(tmp_path: pathlib.Path) -> None:
    """Guards the scanner itself against a silently-vacuous check."""
    planted = tmp_path / "planted.py"
    planted.write_text('def resolve(pack_id: str) -> bool:\n    return pack_id == "rpg"\n')
    assert find_pack_literals(planted) == ["rpg"]

    # A docstring mentioning the same word is NOT a violation.
    clean = tmp_path / "clean.py"
    clean.write_text('"""This module never special-cases the rpg pack."""\n')
    assert find_pack_literals(clean) == []
