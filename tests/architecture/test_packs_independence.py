"""Appendix B's second architecture rule: ``packs/`` may not import from
``packages/core/``. Packs are content plus declarative definitions; the moment a pack
needs a core import, the core is missing an abstraction — that's a signal, not an
inconvenience to work around.

Still trivially green now that F3.7/F3.8 have shipped real pack content (F3.13 adds a
third, swdev): every pack directory is pure JSON, zero ``.py`` files anywhere under
``packs/`` — a design choice this lint would catch the moment it stopped being true.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKS = ROOT / ".plugins"


def _imports_core(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "core" or alias.name.startswith("core."):
                    hits.add(alias.name)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == "core" or node.module.startswith("core."))
        ):
            hits.add(node.module)
    return hits


def test_the_pack_corpus_is_actually_present() -> None:
    """INV-9 is only enforced if there is something to enforce it against.

    Pack content left the repo for a pinned external plugin repo, fetched into gitignored
    `.plugins/`. On a checkout where that fetch failed, the scan below walks an empty
    directory and passes -- a green CI-blocking lint that examined nothing. The
    self-test that follows proves the *scanner* works; this proves the *corpus* exists.
    """
    assert PACKS.is_dir(), (
        f"{PACKS} is missing -- run scripts/fetch_plugins.py. Without it the INV-9 lint "
        "below passes vacuously instead of checking the packs."
    )
    pack_files = list(PACKS.rglob("*.py")) + list(PACKS.rglob("*.json"))
    assert pack_files, (
        f"{PACKS} exists but is empty; the plugin fetch did not land any content, so the "
        "INV-9 lint has nothing to check"
    )


def test_packs_import_nothing_from_core() -> None:
    offenders: list[str] = []
    for path in PACKS.rglob("*.py"):
        hits = _imports_core(path)
        if hits:
            offenders.append(f"{path.relative_to(ROOT)}: imports {sorted(hits)}")

    assert not offenders, (
        "packs/ may not import from packages/core/ (Appendix B). Offending files:\n"
        + "\n".join(offenders)
    )


def test_pack_importing_core_fails_lint(tmp_path: pathlib.Path) -> None:
    """its own acceptance criterion: guards the scanner against being silently
    vacuous, the same way ``test_vocabulary_lint.py``'s self-test does for its lint."""
    planted = tmp_path / "planted_tool.py"
    planted.write_text("from core.entities.mutation import mutate\n")
    assert _imports_core(planted) == {"core.entities.mutation"}
