"""The release stack and the build-from-source stack must stay the same deployment.

``docker/compose.release.yml`` is a second copy of the compose topology whose images
are pulled rather than built. Two copies drift: a variable added to one and not the
other produces a deployment that differs from the tested one in a way nobody sees until
an operator reports a feature missing. These tests are the diff nobody would otherwise
run.

They also assert what makes the release file *releasable* -- it is downloaded on its
own, so anything it expects to find beside it on disk is a file the person following the
instructions does not have.
"""

from __future__ import annotations

import pathlib
import re

import yaml

_DOCKER = pathlib.Path(__file__).resolve().parents[2] / "docker"
_SELFHOST = _DOCKER / "compose.selfhost.yml"
_RELEASE = _DOCKER / "compose.release.yml"

# The one documented difference. The source stack ships an empty ./plugins-local beside
# the compose file; a downloaded compose file has no directory beside it, so the release
# stack carries a named volume instead.
_BIND_MOUNTS_ALLOWED_IN_RELEASE = {"PYRRHULA_ENGINE_SOCKET"}


def _services(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())["services"]


def test_the_two_stacks_run_the_same_services() -> None:
    assert set(_services(_SELFHOST)) == set(_services(_RELEASE))


def test_the_two_stacks_pass_the_same_environment() -> None:
    """Env keys, not values: the values differ by design (the release stack defaults to
    an online first start, because its model cache begins empty)."""
    selfhost = set(_services(_SELFHOST)["migrate"]["environment"])
    release = set(_services(_RELEASE)["migrate"]["environment"])
    assert selfhost == release, (
        f"only in the source stack: {sorted(selfhost - release)}; "
        f"only in the release stack: {sorted(release - selfhost)}"
    )


def test_the_release_stack_builds_nothing() -> None:
    """A `build:` key means the file needs a source tree, which is the one thing the
    person running it does not have."""
    built = [name for name, svc in _services(_RELEASE).items() if "build" in svc]
    assert not built, f"services still built from source in the release stack: {built}"


def test_the_release_stack_mounts_no_file_from_this_repository() -> None:
    """A relative bind mount resolves next to the compose file. Downloaded on its own,
    there is nothing next to it, and compose silently creates an empty directory --
    which is how a stack comes up with no settings file and a search engine that
    answers nothing."""
    offenders = []
    for name, svc in _services(_RELEASE).items():
        for volume in svc.get("volumes", []):
            source = str(volume).split(":")[0]
            if source.startswith((".", "/")) and not any(
                token in str(volume) for token in _BIND_MOUNTS_ALLOWED_IN_RELEASE
            ):
                offenders.append(f"{name}: {volume}")
    assert not offenders, f"host paths in a file meant to travel alone: {offenders}"


def test_every_image_in_the_release_stack_is_pinned_to_one_version() -> None:
    """`:latest` in a release file means two people running the same instructions on
    different days get different software."""
    floating = [
        f"{name}: {svc['image']}"
        for name, svc in _services(_RELEASE).items()
        if "image" in svc and svc["image"].endswith(":latest")
    ]
    assert not floating, f"unpinned images: {floating}"


def test_the_release_default_version_matches_this_source_tree() -> None:
    """The default in the compose file is what someone gets who sets no PYRRHULA_VERSION.
    If it names an older release than this tree, the file ships pointing at the past."""
    pyproject = (_DOCKER.parent / "pyproject.toml").read_text()
    declared = re.search(r'^version = "(.+)"', pyproject, re.M).group(1)
    text = _RELEASE.read_text()
    defaults = set(re.findall(r"\$\{PYRRHULA_VERSION:-([^}]+)\}", text))
    assert defaults, "the release stack no longer carries a default version"
    normalised = {d.replace("-", "") for d in defaults}
    assert normalised == {declared.replace("-", "")}, (
        f"compose defaults to {sorted(defaults)} but this tree is version {declared}"
    )
