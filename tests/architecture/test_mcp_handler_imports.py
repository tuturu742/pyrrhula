"""Architecture lint: nothing under `packages/api/mcp_server/` imports a repo
module directly.

`test_inv1_import_graph.py` already sweeps the whole tree for the same two modules. This
is the narrower, surface-specific companion -- kept separate because the MCP surface is
where a shortcut is most tempting ("it's just a read, and there's no assembler here") and
because a failure here should say *which rule about MCP handlers* was broken rather than
pointing at a repo-wide allowlist.

It also asserts the positive half of "handlers are thin": each tool module reaches the
database only through a service, never by constructing its own session.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
MCP_DIR = ROOT / "packages" / "api" / "mcp_server"

_FORBIDDEN_MODULES = {"core.knowledge.repo", "core.secrets.repo"}
# A handler that opens its own session is a handler doing a service's job -- and the place
# a scope filter or an audit row gets forgotten.
_FORBIDDEN_NAMES = {"tenant_scope", "unscoped_session"}


def _imports(path: pathlib.Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            names.update(a.name for a in node.names)
            modules.update(f"{node.module}.{a.name}" for a in node.names)
    return modules, names


def _handler_files() -> list[pathlib.Path]:
    return [p for p in MCP_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def test_mcp_handlers_have_no_direct_repo_imports() -> None:
    assert _handler_files(), f"expected handler modules under {MCP_DIR}"
    offenders: list[str] = []
    for path in _handler_files():
        modules, _names = _imports(path)
        hits = modules & _FORBIDDEN_MODULES
        if hits:
            offenders.append(f"{path.relative_to(ROOT)}: imports {sorted(hits)}")

    assert not offenders, (
        "MCP handlers must reach knowledge/secrets through a service, never a repo "
        "module -- INV-1 applies to this surface exactly as it does to every other:\n"
        + "\n".join(offenders)
    )


def test_mcp_handlers_do_not_open_their_own_database_sessions() -> None:
    offenders: list[str] = []
    for path in _handler_files():
        _modules, names = _imports(path)
        hits = names & _FORBIDDEN_NAMES
        if hits:
            offenders.append(f"{path.relative_to(ROOT)}: imports {sorted(hits)}")

    assert not offenders, (
        "an MCP handler that opens its own session is doing a service's job, and is where "
        "a scope filter or an audit row gets forgotten:\n" + "\n".join(offenders)
    )
