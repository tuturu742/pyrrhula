"""The running platform's version, read from one place.

``pyproject.toml`` is where the number lives. This reads it back through the installed
package metadata, falling back to the file itself for a checkout that was never
installed, so nothing else in the tree carries a copy that can drift. Two copies did:
the API's OpenAPI banner and the bundle exporter each said "0.1.0" on their own, and a
release that bumped one and not the other would have stamped bundles with the wrong
version forever.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version
from pathlib import Path


def _from_pyproject() -> str | None:
    for candidate in (
        Path(__file__).resolve().parents[2] / "pyproject.toml",
        Path("/app/pyproject.toml"),
    ):
        try:
            with candidate.open("rb") as handle:
                return str(tomllib.load(handle)["project"]["version"])
        except (OSError, KeyError, ValueError):
            continue
    return None


def _resolve() -> str:
    try:
        return _installed_version("pyrrhula")
    except PackageNotFoundError:
        pass
    return _from_pyproject() or "0.0.0"


APP_VERSION: str = _resolve()
