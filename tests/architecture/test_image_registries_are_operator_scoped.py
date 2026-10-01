"""Architecture lint: container registries and the runtime-image policy are the operator's.

A declared registry carries a credential and defines which tenant owns which part of it;
the allowlist decides where any runtime image may come from. Both are deployment
decisions. The line only holds if nothing a model can drive, and nothing a workflow pack
ships, can reach the code that changes them -- the same line the harness registry holds.
"""

from __future__ import annotations

import ast
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"

_MUTATORS = {
    "create_registry",
    "update_registry",
    "delete_registry",
    "set_registry_credential",
    "clear_registry_credential",
    "registry_read_credential",
    "set_runtime_image_allowlist",
}

_MODEL_FACING = [
    PACKAGES / "core" / "process" / "session_container_tools.py",
    PACKAGES / "core" / "process" / "session_delegation_tools.py",
    PACKAGES / "core" / "process" / "session_remote_tools.py",
    PACKAGES / "core" / "process" / "session_entity_tools.py",
    PACKAGES / "core" / "agents" / "assistant_chat.py",
    # The admin console's own assistant proposes writes too; a registry is not one of them.
    PACKAGES / "core" / "admin" / "assistant.py",
]

_PACK_KEYS = {"image_registries", "runtime_image_allowlist"}


def _names_used(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("core.images"):
            names.update(alias.name for alias in node.names)
    return names


def test_no_model_facing_code_can_change_a_registry_or_the_allowlist() -> None:
    checked = [p for p in _MODEL_FACING if p.is_file()]
    assert checked, "none of the model-facing modules exist -- this test checks nothing"
    offenders = {
        p.name: sorted(_names_used(p) & _MUTATORS) for p in checked if _names_used(p) & _MUTATORS
    }
    assert not offenders, f"model-facing code that can change registries: {offenders}"


def test_only_the_admin_routes_call_the_mutators_outside_core_images() -> None:
    """One door per write. A second caller is a second place to forget the audit row or
    the platform-admin check."""
    allowed = {
        PACKAGES / "api" / "routes" / "admin_images.py",
    }
    offenders: dict[str, list[str]] = {}
    for path in PACKAGES.rglob("*.py"):
        if "/tests/" in str(path) or path.is_relative_to(PACKAGES / "core" / "images"):
            continue
        if path in allowed:
            continue
        used = _names_used(path) & _MUTATORS
        if used:
            offenders[str(path.relative_to(ROOT))] = sorted(used)
    assert not offenders, f"registry mutators called outside the admin API: {offenders}"


def test_only_core_images_writes_the_registry_table() -> None:
    writers = []
    for path in PACKAGES.rglob("*.py"):
        if "/tests/" in str(path) or path.is_relative_to(PACKAGES / "core" / "images"):
            continue
        text = path.read_text()
        if "ImageRegistryRow(" in text or "INTO image_registry" in text:
            writers.append(str(path.relative_to(ROOT)))
    assert not writers, f"modules writing image_registry directly: {writers}"


def test_no_workflow_pack_declares_registries_or_an_allowlist() -> None:
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
            if isinstance(data, dict) and _PACK_KEYS & set(data):
                offenders.append(str(path.relative_to(ROOT)))
    assert scanned, "no pack json was scanned; run scripts/fetch_plugins.py first"
    assert not offenders, f"pack content declaring registry policy: {offenders}"
