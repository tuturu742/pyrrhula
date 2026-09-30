"""Architecture lint: a harness spec carries a shell command, so only an operator may
introduce one.

A harness spec's ``command`` runs in the execution environment. That is the same trust
level as ``setup_cmds`` and ``test_cmd``, which are already operator-supplied shell -- and
it is emphatically *not* the level CLAUDE.md rule 10 governs, which is logic supplied by
session participants. The line only holds if nothing downstream of a model or a pack can
reach the registry.

Two ways it could be crossed, both checked here:

- **a workflow pack declaring one.** Packs are content, not code (rule 9). A pack that
  could ship a harness would be shipping a command line into every tenant that installs
  it, which is the supply-chain shape this project does not have.
- **a model-facing tool writing one.** The in-turn tools are the surface a model can
  reach; none of them may register, withhold or otherwise edit a harness.
"""

from __future__ import annotations

import ast
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "packages" / "core" / "harness" / "registry.py"
SETTING_KEY = "harnesses"

# The registry's own mutators. Anything else calling them is what this is looking for.
_MUTATORS = {"register_harness", "withhold_harness", "remove_harness", "validate_spec"}

# Where a model's tool calls are handled. These run with whatever a model asked for, so a
# harness mutator reachable from one would put a command line in a model's gift.
_MODEL_FACING = [
    ROOT / "packages" / "core" / "process" / "session_container_tools.py",
    ROOT / "packages" / "core" / "process" / "session_delegation_tools.py",
    ROOT / "packages" / "core" / "process" / "session_remote_tools.py",
    ROOT / "packages" / "core" / "process" / "session_entity_tools.py",
    ROOT / "packages" / "core" / "agents" / "assistant_chat.py",
]


def _called_names(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("core.harness"):
            names.update(alias.name for alias in node.names)
    return names


def test_no_model_facing_tool_can_register_a_harness() -> None:
    """A model may only *select* a harness key that an operator already registered."""
    offenders = {}
    for path in _MODEL_FACING:
        if not path.is_file():
            continue
        crossed = _called_names(path) & _MUTATORS
        if crossed:
            offenders[path.name] = sorted(crossed)
    assert not offenders, (
        f"model-facing tools that can change a harness registration: {offenders}. "
        "A harness command runs in the execution environment; only an operator may set one."
    )


def test_no_workflow_pack_declares_a_harness() -> None:
    """Packs are content, not code. A pack shipping a harness would be shipping a command
    line into every tenant that installs it."""
    offenders = []
    scanned = 0
    for base in (ROOT / ".plugins", ROOT / "builtin-workflows"):
        if not base.is_dir():
            continue
        for path in base.rglob("*.json"):
            try:
                data = json.loads(path.read_text())
            except (ValueError, OSError):
                continue
            scanned += 1
            if isinstance(data, dict) and SETTING_KEY in data:
                offenders.append(str(path.relative_to(ROOT)))
    # The corpus has to be there, or this passes by scanning nothing -- the same vacuous
    # green that let a CI-blocking suite report success while running no tests.
    assert scanned, (
        "no pack json was scanned; run scripts/fetch_plugins.py, because a guard over an "
        "empty corpus proves nothing"
    )
    assert not offenders, f"pack content declaring harnesses: {offenders}"


def test_the_registry_is_the_only_writer_of_the_setting() -> None:
    """One door. If another module wrote ``tenant.settings['harnesses']`` directly it
    would bypass validate_spec, and with it the placeholder whitelist that keeps a spec
    from naming anything the script builder does not supply."""
    writers = []
    for path in (ROOT / "packages").rglob("*.py"):
        if path == REGISTRY or "/tests/" in str(path):
            continue
        text = path.read_text()
        if "SETTING_KEY: " in text and "harness" in text.lower():
            writers.append(str(path.relative_to(ROOT)))
        if f'"{SETTING_KEY}"]' in text and "settings" in text:
            writers.append(str(path.relative_to(ROOT)))
    assert not writers, (
        f"modules writing the harness setting outside the registry: {sorted(set(writers))}"
    )
