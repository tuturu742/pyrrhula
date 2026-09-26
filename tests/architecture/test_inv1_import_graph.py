"""INV-1: no stored text reaches a model except through ``ContextAssembler.assemble()``.
Enforced structurally: only ``core.assembler`` and ``core.overseer`` may import
``core.knowledge.repo`` or ``core.secrets.repo``. "This is the single highest-value test
in the repository"  — every leak bug in a system like this is "some new
feature read the knowledge table directly because it was convenient."

Uses the AST rather than a regex/string search so `from core.knowledge import repo` and
`from core.knowledge.repo import Foo` and `import core.knowledge.repo as kr` are all
caught the same way, regardless of how the forbidden import is spelled.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"

_FORBIDDEN_MODULES = {"core.knowledge.repo", "core.secrets.repo"}

_ALLOWED_DIRS = (
    PACKAGES / "core" / "assembler",
    PACKAGES / "core" / "overseer",
)


def _imported_module_names(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            for alias in node.names:
                modules.add(f"{node.module}.{alias.name}")
    return modules


def _is_allowed(path: pathlib.Path) -> bool:
    if any(d in path.parents for d in _ALLOWED_DIRS):
        return True
    # The repo modules importing themselves, and tests exercising them directly, are not
    # the production read-path INV-1 protects.
    if "tests" in path.parts:
        return True
    return path.name == "repo.py" and path.parent.name in {"knowledge", "secrets"}


def test_only_assembler_and_overseer_import_knowledge_or_secrets_repo() -> None:
    offenders: list[str] = []
    for path in PACKAGES.rglob("*.py"):
        if _is_allowed(path):
            continue
        forbidden_hits = _imported_module_names(path) & _FORBIDDEN_MODULES
        if forbidden_hits:
            offenders.append(f"{path.relative_to(ROOT)}: imports {sorted(forbidden_hits)}")

    assert not offenders, (
        "INV-1 violation: only core/assembler/ and core/overseer/ may import "
        "core.knowledge.repo or core.secrets.repo. Offending files:\n" + "\n".join(offenders)
    )
