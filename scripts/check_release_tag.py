#!/usr/bin/env python3
"""Refuse a release tag that disagrees with the version in ``pyproject.toml``.

One release wears three spellings -- the git tag ``v0.1.0-rc1``, the image tag
``0.1.0-rc1`` and PEP 440's ``0.1.0rc1`` in ``pyproject.toml`` -- and nothing but this
check stops a tag from naming one release while the images inside it are another.

Run by ``.github/workflows/release.yml`` before anything is built or pushed. Lives here
rather than inline in the workflow so it can be tested, and so it needs no dependency
the release runner would have to install first.

    scripts/check_release_tag.py v0.1.0-rc1     # prints the image tag, exits 0
"""

from __future__ import annotations

import pathlib
import re
import sys

# PEP 440 normalisation, only as far as this needs it: a pre-release separator is
# optional, so 0.1.0-rc1, 0.1.0.rc1, 0.1.0_rc1 and 0.1.0rc1 are one version. Anything
# stricter would need the `packaging` distribution, and a release check that has to
# install something before it can run is a release check with an outage in it.
_PRE_SEPARATOR = re.compile(r"[-_.]?(a|b|c|rc|alpha|beta|pre|preview|post|dev)[-_.]?", re.I)


def normalise(version: str) -> str:
    return _PRE_SEPARATOR.sub(lambda m: m.group(1).lower(), version.strip().lower())


def declared_version(pyproject: pathlib.Path) -> str:
    match = re.search(r'^version = "(.+)"', pyproject.read_text(), re.M)
    if match is None:
        raise SystemExit(f"no version in {pyproject}")
    return match.group(1)


def image_tag(git_tag: str) -> str:
    return git_tag.removeprefix("v")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: check_release_tag.py <git tag, e.g. v0.1.0-rc1>")
    tag = image_tag(argv[1])
    declared = declared_version(pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml")
    if normalise(tag) != normalise(declared):
        raise SystemExit(
            f"::error::tag {tag!r} and pyproject version {declared!r} are different "
            f"releases; bump one to match the other before tagging"
        )
    print(tag)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
