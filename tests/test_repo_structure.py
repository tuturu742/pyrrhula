"""Guards the repo skeleton itself.

The CI-blocking suites (tests/isolation, tests/architecture, tests/replay, tests/leak,
tests/packs) each carry their own content-level guards (e.g. the per-table coverage
check). This test only makes sure their directories — and the rest of the repository
layout — can't quietly disappear.
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

REQUIRED_DIRS = [
    "tests/isolation",
    "tests/architecture",
    "tests/replay",
    "tests/leak",
    "tests/packs",
    "packages/core",
    "packages/adapters",
    "packages/api",
    "packages/worker",
    "packages/eval",
    # Pack content moved OUT of the tree into pinned plugin repositories
    # (deploy/plugins.json -> .plugins/); the in-tree survivor is the default pack
    # every deployment ships with.
    "builtin-workflows/default",
    "web",
    "migrations",
    "docker",
]


def test_required_directories_exist() -> None:
    missing = [d for d in REQUIRED_DIRS if not (ROOT / d).is_dir()]
    assert not missing, f"required directories missing: {missing}"


def test_only_one_application_dockerfile() -> None:
    """One APPLICATION image, config not forks -- catch a second app
    Dockerfile early. Optional deploy-side sidecars (deploy/mcp-sidecars: the Godot
    engine bridge, the ComfyUI bridge) are separate workloads an operator runs beside
    Pyrrhula, never variants of the app image, so they live under deploy/ and are
    excluded here by location rather than by name."""
    dockerfiles = [
        p
        for p in ROOT.glob("**/*Dockerfile*")
        if ".git" not in p.parts and "deploy" not in p.parts and "node_modules" not in p.parts
    ]
    # ONE deployment's images, not application variants: the app itself and the static
    # web bundle. SearXNG is not ours to build -- the upstream image runs with our
    # settings file mounted beside it (docker/compose.release.yml), and k8s inlines the
    # same settings in a ConfigMap, so a Dockerfile whose whole content was that file
    # would be a third copy of it.
    names = sorted(p.name for p in dockerfiles)
    assert names == ["Dockerfile", "web.Dockerfile"], (
        f"unexpected application Dockerfiles: {[str(p) for p in dockerfiles]}"
    )
