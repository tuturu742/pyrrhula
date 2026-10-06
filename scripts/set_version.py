#!/usr/bin/env python3
"""Set the platform version everywhere it is written, in one step.

    scripts/set_version.py 0.1.1        # a release: source version AND published-image refs
    scripts/set_version.py 0.2.0.dev0   # development: source version only

Two kinds of strings carry a version, and they move at different times:

* **The source version** -- ``pyproject.toml`` (read back by ``core.version``, so the API,
  the OpenAPI banner and exported bundles follow it), ``uv.lock``'s own entry, and
  ``web/package.json`` -- says what this tree *is*. It moves on every bump.
* **Published-image references** -- the release compose file's default tag, the release
  installer's fallback, and the version examples in the install docs -- name images that
  can be *pulled*. They move only to a final or pre-release version, never to a ``.devN``:
  no image of a dev version exists, and pointing at one would send every no-checkout
  install to a 404 (``tests/architecture/test_compose_release_parity.py`` holds both
  rules).

Before this script, a release was a checklist of eight files in a memory file, and one
release (rc1) shipped with ``web/package.json`` three versions behind.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(rc)(\d+)|\.(dev)(\d+))?$")


def forms(version: str) -> dict[str, str]:
    """The three spellings of one version: PEP 440, image tag, npm semver."""
    match = _VERSION.match(version)
    if match is None:
        raise SystemExit(
            f"{version!r} is not X.Y.Z, X.Y.ZrcN or X.Y.Z.devN (PEP 440, as pyproject spells it)"
        )
    major, minor, patch, rc, rc_n, dev, dev_n = match.groups()
    base = f"{major}.{minor}.{patch}"
    if rc:
        return {"pep440": version, "image": f"{base}-rc{rc_n}", "npm": f"{base}-rc.{rc_n}"}
    if dev:
        return {"pep440": version, "image": "", "npm": f"{base}-dev.{dev_n}"}
    return {"pep440": version, "image": base, "npm": base}


def _sub(path: pathlib.Path, pattern: str, replacement: str, *, flags: int = 0) -> int:
    text = path.read_text()
    new, count = re.subn(pattern, replacement, text, flags=flags)
    if count == 0:
        raise SystemExit(f"{path}: pattern not found: {pattern}")
    path.write_text(new)
    return count


def set_source(root: pathlib.Path, f: dict[str, str]) -> list[str]:
    _sub(root / "pyproject.toml", r'^version = "[^"]+"', f'version = "{f["pep440"]}"', flags=re.M)
    _sub(root / "uv.lock", r'(name = "pyrrhula"\nversion = )"[^"]+"', rf'\g<1>"{f["pep440"]}"')
    _sub(root / "web/package.json", r'("version": )"[^"]+"', rf'\g<1>"{f["npm"]}"')
    return ["pyproject.toml", "uv.lock", "web/package.json"]


def published_version(root: pathlib.Path) -> str:
    defaults = set(
        re.findall(
            r"\$\{PYRRHULA_VERSION:-([^}]+)\}", (root / "docker/compose.release.yml").read_text()
        )
    )
    if len(defaults) != 1:
        raise SystemExit(f"compose.release.yml names {sorted(defaults)}; expected one version")
    return defaults.pop()


def set_published(root: pathlib.Path, tag: str) -> list[str]:
    old = re.escape(published_version(root))
    edits = [
        (
            "docker/compose.release.yml",
            rf"(\$\{{PYRRHULA_VERSION:-){old}(\}})",
            rf"\g<1>{tag}\g<2>",
        ),
        ("deploy/installers/release.sh", rf'(FALLBACK_VERSION="){old}(")', rf"\g<1>{tag}\g<2>"),
        ("deploy/installers/release.sh", rf"(sh -s -- ){old}\b", rf"\g<1>{tag}"),
        ("install.sh", rf"(--from-registry=){old}\b", rf"\g<1>{tag}"),
        ("docs/install.md", rf"(sh -s -- ){old}\b", rf"\g<1>{tag}"),
        ("docs/install.md", rf"(VERSION=){old}\b", rf"\g<1>{tag}"),
        ("docs/install.md", rf"(--from-registry=){old}\b", rf"\g<1>{tag}"),
    ]
    for rel, pattern, replacement in edits:
        _sub(root / rel, pattern, replacement)
    return sorted({rel for rel, _, _ in edits})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="PEP 440: X.Y.Z, X.Y.ZrcN or X.Y.Z.devN")
    parser.add_argument(
        "--root", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args(argv)
    f = forms(args.version)
    changed = set_source(args.root, f)
    if f["image"]:
        changed += set_published(args.root, f["image"])
    print(f"version {f['pep440']} (image {f['image'] or 'unchanged: dev'}, web {f['npm']})")
    for rel in changed:
        print(f"  updated {rel}")
    print(
        "  by hand: CHANGELOG.md (move Unreleased under the new heading and add its link),\n"
        "  and the status line in README.md and FAQ.md"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
