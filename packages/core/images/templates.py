"""Starting points for an image, offered in the editor.

A template is a Dockerfile plus, optionally, a harness to bake in. The harness install is
**never written into a template's text**: it is appended by ``core.images.builds`` from
the operator's own harness setup commands, so a template cannot drift from the harness
version the platform actually runs -- and the harness templates are generated from
``BUILTIN_HARNESSES`` for the same reason (a test holds the two together).

Every template must pass the Dockerfile validator as written.
"""

from __future__ import annotations

from typing import Any

_GIT = (
    "RUN apt-get update \\\n"
    " && apt-get install -y --no-install-recommends git ca-certificates \\\n"
    " && rm -rf /var/lib/apt/lists/*\n"
)

_BASE: list[dict[str, Any]] = [
    {
        "key": "debian-git",
        "label": "Debian with git",
        "description": "The smallest useful start: add the tools your repository needs.",
        "dockerfile": "FROM docker.io/library/debian:bookworm\n" + _GIT,
        "harness_key": "",
    },
    {
        "key": "python",
        "label": "Python 3.12",
        "description": "Python with pip and git; add your system packages below.",
        "dockerfile": "FROM docker.io/library/python:3.12-bookworm\n" + _GIT,
        "harness_key": "",
    },
]

# A harness installed with npm needs Node in the image; these are the bases that have it.
_NODE_BASE = "docker.io/library/node:20-bookworm"


def _harness_templates() -> list[dict[str, Any]]:
    from core.harness.registry import BUILTIN_HARNESSES

    out: list[dict[str, Any]] = []
    for key, spec in sorted(BUILTIN_HARNESSES.items()):
        setup = [str(c) for c in spec.get("setup_cmds") or []]
        if not spec.get("enabled", True) or not setup:
            continue
        if not all(cmd.startswith("npm ") for cmd in setup):
            continue  # only npm-installed harnesses have a known base to offer
        out.append(
            {
                "key": f"node-{key}",
                "label": f"Node 20 with {key} baked in",
                "description": (
                    f"Node and git, with the {key} harness installed by the platform "
                    f"({spec.get('version') or 'the configured version'}), so delegations "
                    "skip installing it on every run."
                ),
                "dockerfile": f"FROM {_NODE_BASE}\n" + _GIT,
                "harness_key": key,
            }
        )
    return out


def list_templates() -> list[dict[str, Any]]:
    return [*_BASE, *_harness_templates()]
