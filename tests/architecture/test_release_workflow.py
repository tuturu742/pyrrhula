"""The release workflow's own guards, tested.

A release runs once per tag and is the least convenient place to discover a typo: a
guard that can never match refuses every tag, and a guard that matches the wrong thing
publishes from a commit nobody verified. Both were true of the first draft of this
workflow -- it looked for a run named ``ci`` when the workflow is named ``CI``.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_RELEASE = _ROOT / ".github/workflows/release.yml"
_CI = _ROOT / ".github/workflows/ci.yml"
_CHECK = _ROOT / "scripts/check_release_tag.py"

sys.path.insert(0, str(_ROOT / "scripts"))
from check_release_tag import declared_version, image_tag, normalise  # noqa: E402


@pytest.mark.parametrize(
    ("tag", "declared"),
    [
        ("v0.1.0-rc1", "0.1.0rc1"),
        ("v0.1.0rc1", "0.1.0rc1"),
        ("v0.1.0.rc1", "0.1.0rc1"),
        ("v1.2.3", "1.2.3"),
        ("v1.2.3-beta2", "1.2.3beta2"),
    ],
)
def test_one_release_may_be_spelled_several_ways(tag: str, declared: str) -> None:
    assert normalise(image_tag(tag)) == normalise(declared)


@pytest.mark.parametrize(
    ("tag", "declared"),
    [("v0.2.0", "0.1.0rc1"), ("v0.1.0", "0.1.0rc1"), ("v0.1.0-rc2", "0.1.0rc1")],
)
def test_a_different_release_is_a_different_release(tag: str, declared: str) -> None:
    assert normalise(image_tag(tag)) != normalise(declared)


def test_the_check_refuses_a_mismatched_tag() -> None:
    """End to end, the way the workflow calls it: a non-zero exit is what stops the
    build, so a script that printed a complaint and exited 0 would publish anyway."""
    result = subprocess.run([sys.executable, str(_CHECK), "v9.9.9"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "different releases" in result.stderr

    ok = subprocess.run(
        [sys.executable, str(_CHECK), f"v{declared_version(_ROOT / 'pyproject.toml')}"],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0, ok.stderr


def test_the_green_ci_guard_names_a_workflow_that_exists() -> None:
    """The guard selects the CI run by workflow file. Matching on the display name is
    what broke before: the workflow is named "CI", the guard looked for "ci", no run
    ever matched, and every tag would have been refused."""
    steps = yaml.safe_load(_RELEASE.read_text())["jobs"]["publish"]["steps"]
    guard = next(s for s in steps if "green ci run" in s.get("name", "").lower())
    assert ".github/workflows/ci.yml" in guard["run"]
    assert _CI.is_file()


def test_every_published_image_is_built_in_ci_too() -> None:
    """An image CI never builds is an image whose first build is the release, which is
    the one build with no chance to fix it."""
    release_files = {
        step["with"]["file"]
        for step in yaml.safe_load(_RELEASE.read_text())["jobs"]["publish"]["steps"]
        if step.get("uses", "").startswith("docker/build-push-action")
    }
    ci_files = {
        step["with"]["file"]
        for step in yaml.safe_load(_CI.read_text())["jobs"]["build"]["steps"]
        if step.get("uses", "").startswith("docker/build-push-action")
    }
    assert release_files <= ci_files, f"published but never built in CI: {release_files - ci_files}"
