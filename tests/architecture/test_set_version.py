"""scripts/set_version.py on a copy of the files it edits: a dev bump moves only the source
version; a release moves the published-image references too, and the result satisfies the
compose parity rules either way."""

from __future__ import annotations

import pathlib
import re
import shutil
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

from set_version import forms, main, published_version  # noqa: E402

_FILES = [
    "pyproject.toml",
    "uv.lock",
    "web/package.json",
    "docker/compose.release.yml",
    "deploy/installers/release.sh",
    "install.sh",
    "docs/install.md",
]


@pytest.fixture
def tree(tmp_path: pathlib.Path) -> pathlib.Path:
    for rel in _FILES:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_ROOT / rel, tmp_path / rel)
    return tmp_path


def _pyproject_version(root: pathlib.Path) -> str:
    return re.search(r'^version = "(.+)"', (root / "pyproject.toml").read_text(), re.M).group(1)


def test_forms() -> None:
    assert forms("0.1.1") == {"pep440": "0.1.1", "image": "0.1.1", "npm": "0.1.1"}
    assert forms("0.2.0rc1") == {"pep440": "0.2.0rc1", "image": "0.2.0-rc1", "npm": "0.2.0-rc.1"}
    assert forms("0.2.0.dev0")["image"] == "" and forms("0.2.0.dev0")["npm"] == "0.2.0-dev.0"
    with pytest.raises(SystemExit):
        forms("v0.1.1")


def test_a_dev_bump_leaves_the_published_references_alone(tree: pathlib.Path) -> None:
    before = {rel: (tree / rel).read_text() for rel in _FILES[3:]}
    main(["9.9.9.dev3", "--root", str(tree)])
    assert _pyproject_version(tree) == "9.9.9.dev3"
    assert '"version": "9.9.9-dev.3"' in (tree / "web/package.json").read_text()
    assert 'name = "pyrrhula"\nversion = "9.9.9.dev3"' in (tree / "uv.lock").read_text()
    assert {rel: (tree / rel).read_text() for rel in _FILES[3:]} == before


def test_a_release_moves_every_published_reference(tree: pathlib.Path) -> None:
    old = published_version(tree)
    main(["9.9.9", "--root", str(tree)])
    assert published_version(tree) == "9.9.9"
    for rel in _FILES[3:]:
        text = (tree / rel).read_text()
        assert "9.9.9" in text, rel
        for pattern in (
            rf"sh -s -- {re.escape(old)}\b",
            rf"VERSION={re.escape(old)}\b",
            rf"--from-registry={re.escape(old)}\b",
            rf'FALLBACK_VERSION="{re.escape(old)}"',
        ):
            assert not re.search(pattern, text), (rel, pattern)
