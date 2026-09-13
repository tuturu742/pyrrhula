"""Guards the repo skeleton itself.

The CI-blocking suites (tests/isolation, tests/architecture, tests/replay, tests/leak,
tests/packs) are owned by later tasks (T0.4, T0.5, C1.3, Phase 2, Phase 3 respectively)
and are empty until those tasks land. This test makes sure their directories — and the
rest of the Appendix B layout — can't quietly disappear in the meantime; each owning task
adds the real, content-level guard (e.g. T0.4's per-table coverage check).
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
    """One APPLICATION image, config not forks (plan §13.8) -- catch a second app
    Dockerfile early. Optional deploy-side sidecars (deploy/mcp-sidecars: the Godot
    engine bridge, the ComfyUI bridge) are separate workloads an operator runs beside
    Pyrrhula, never variants of the app image, so they live under deploy/ and are
    excluded here by location rather than by name."""
    dockerfiles = [
        p
        for p in ROOT.glob("**/*Dockerfile*")
        if ".git" not in p.parts and "deploy" not in p.parts and "node_modules" not in p.parts
    ]
    # ONE deployment's images, not application variants: the app itself, the static web
    # bundle, and the bundled SearXNG used by the web-search preset.
    names = sorted(p.name for p in dockerfiles)
    assert names == ["Dockerfile", "searxng.Dockerfile", "web.Dockerfile"], (
        f"unexpected application Dockerfiles: {[str(p) for p in dockerfiles]}"
    )
