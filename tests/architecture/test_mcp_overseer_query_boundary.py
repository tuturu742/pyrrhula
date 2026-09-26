"""its own acceptance criterion: the `overseer.query` MCP tool handler contains no
import of `core.secrets.repo` -- `OverseerService` is the only boundary it may cross to
reach a secret. This is a narrower, tool-specific companion to
`test_inv1_import_graph.py`'s repo-wide sweep (which already proves the same thing at
every path, `packages/api/mcp_server/` included) -- kept as its own test so this file's
own name states the tool's contract directly, and survives on its own even if the
repo-wide lint's allowlist ever changes shape.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / "packages" / "api" / "mcp_server" / "tools" / "overseer_query.py"

_FORBIDDEN_MODULES = {"core.knowledge.repo", "core.secrets.repo"}


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


def test_mcp_handler_does_not_import_secrets_repo() -> None:
    assert TOOL_PATH.is_file(), f"expected {TOOL_PATH} to exist"
    forbidden_hits = _imported_module_names(TOOL_PATH) & _FORBIDDEN_MODULES
    assert not forbidden_hits, (
        f"{TOOL_PATH.relative_to(ROOT)} imports {sorted(forbidden_hits)} directly -- "
        "OverseerService is the only boundary this tool may cross to reach a secret"
    )
