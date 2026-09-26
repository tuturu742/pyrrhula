"""Architecture lint: `core/actions/delegation.py` reaches stored text only
through `ContextAssembler.assemble()`.

The task states the rule and the reason: "A second, ad-hoc brief-assembly path kills INV-1
and INV-8 across the delegation boundary." `test_inv1_import_graph.py` already forbids the
repo imports tree-wide; this adds the delegation-specific half — that the module *calls
assemble*, exactly once, and builds no brief any other way.

A runtime leak test (`tests/leak/test_delegation_brief.py`) shows today's brief excludes a
concealed secret. This shows there is no second path for tomorrow's to be built along.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
DELEGATION = ROOT / "packages" / "core" / "actions" / "delegation.py"

_FORBIDDEN_MODULES = {"core.knowledge.repo", "core.secrets.repo"}
# Reading knowledge or secrets *at all* outside the assembler is the shortcut this guards.
_FORBIDDEN_NAMES = {
    "KnowledgeEntry",
    "KnowledgeChunk",
    "SecretRow",
    "search_sparse",
    "search_dense",
    "fetch_chunk_texts",
}


def _tree() -> ast.Module:
    return ast.parse(DELEGATION.read_text(), filename=str(DELEGATION))


def test_delegation_builds_its_brief_only_through_the_assembler() -> None:
    assert DELEGATION.is_file(), f"expected {DELEGATION} to exist"
    tree = _tree()

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "assemble"
    ]
    assert len(calls) == 1, (
        f"expected exactly one assemble() call in delegation.py, found {len(calls)} -- the "
        "brief has one source, and a second one is how INV-1 and INV-8 stop holding across "
        "the delegation boundary"
    )

    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            names.update(a.name for a in node.names)

    assert not (modules & _FORBIDDEN_MODULES), (
        f"delegation.py imports {sorted(modules & _FORBIDDEN_MODULES)} -- INV-1 reserves "
        "those for the assembler and the overseer"
    )
    assert not (names & _FORBIDDEN_NAMES), (
        f"delegation.py imports {sorted(names & _FORBIDDEN_NAMES)} -- reading knowledge or "
        "secrets directly is the side channel the brief must not have"
    )


def test_delegation_reconciles_only_through_read_only_lookups() -> None:
    """The reconcile path must not be able to *cause* the thing it is checking for. Both
    lookup tool names are module constants, and the dispatch tool name is a third — so a
    reconcile that reached for the dispatcher would be visibly reaching for a different
    constant."""
    source = DELEGATION.read_text()
    reconcile_start = source.index("async def reconcile(")
    reconcile_end = source.index("async def delegate_work_item(")
    body = source[reconcile_start:reconcile_end]

    assert "LOOKUP_BRANCH_TOOL" in body
    assert "DELEGATE_TOOL" not in body, (
        "the reconcile path references the dispatch tool -- looking up whether work "
        "happened must never be able to make it happen"
    )
