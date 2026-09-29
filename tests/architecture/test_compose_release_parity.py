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

_INSTALLER = pathlib.Path(__file__).resolve().parents[2] / "deploy/installers/release.sh"


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


def _relative_bind_sources(path: pathlib.Path) -> set[str]:
    """Bind mounts whose source is a path beside the compose file, as opposed to a named
    volume or a host path the operator supplies through .env."""
    sources = set()
    for svc in _services(path).values():
        for volume in svc.get("volumes", []):
            source = str(volume).split(":")[0]
            if source.startswith("./"):
                sources.add(source.removeprefix("./"))
    return sources


def test_the_installer_downloads_every_file_the_release_stack_mounts() -> None:
    """A relative bind mount resolves next to the compose file, and the person running
    this has only what the installer put there. A file mounted but never downloaded is
    a container that exits 127 on an install that otherwise looked fine -- loud, but
    loud at the worst moment.

    This is the check that replaced "the release stack mounts nothing from this
    repository". Mounting nothing was one way to be self-contained; fetching what you
    mount is another, and it does not cost 266MB of republished SearXNG per release."""
    mounted = _relative_bind_sources(_RELEASE)
    installer = _INSTALLER.read_text()
    missing = {f for f in mounted if f not in installer}
    assert not missing, (
        f"{_RELEASE.name} mounts {sorted(missing)}, which {_INSTALLER.name} never "
        f"downloads. Add it to the fetch loop or stop mounting it."
    )


def test_every_file_the_release_stack_mounts_exists_to_be_downloaded() -> None:
    """The installer fetches these from docker/ at the release tag. A rename here that
    misses the installer produces a release whose install path 404s."""
    for name in _relative_bind_sources(_RELEASE):
        assert (_DOCKER / name).is_file(), (
            f"{name} is mounted and downloaded, but is not in docker/"
        )


# The one image we deliberately let float, and why. SearXNG is a search frontend we do
# not control: engines break and get blocked, and a pinned copy degrades quietly between
# our releases. The source stack floats it too, so the two stacks agree. Anything else
# floating is an accident.
_FLOATING_BY_DESIGN = {"docker.io/searxng/searxng:latest"}


def test_every_image_we_publish_is_pinned_to_one_version() -> None:
    """`:latest` on our own images would make PYRRHULA_VERSION a lie -- two people
    running the same instructions on different days would get different software while
    both believing they ran the version in .env."""
    ours = {
        f"{name}: {svc['image']}"
        for name, svc in _services(_RELEASE).items()
        if "image" in svc
        and svc["image"].startswith("ghcr.io/")
        and "${PYRRHULA_VERSION" not in svc["image"]
    }
    assert not ours, f"images of ours not pinned to PYRRHULA_VERSION: {sorted(ours)}"


def test_any_third_party_image_that_floats_is_one_we_chose_to_float() -> None:
    floating = {
        svc["image"]
        for svc in _services(_RELEASE).values()
        if "image" in svc and svc["image"].endswith(":latest")
    }
    assert floating <= _FLOATING_BY_DESIGN, (
        f"unpinned images nobody decided to unpin: {sorted(floating - _FLOATING_BY_DESIGN)}"
    )


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


def test_the_search_settings_are_the_same_on_every_deploy_path() -> None:
    """Those 82 lines exist in two places -- docker/searxng-settings.yml, which compose
    mounts and the release installer downloads, and a ConfigMap inlined in the k8s base.
    They are measured engine selection, re-derived only by re-testing every engine from
    a real host, so a copy that drifts is a deployment quietly searching a different
    web. Compared as YAML, because indentation is not the thing that matters.

    There used to be a third copy, in a docker/searxng.Dockerfile that nothing built."""
    k8s = pathlib.Path(__file__).resolve().parents[2] / "deploy/k8s/base/searxng.yaml"
    configmap = next(
        d
        for d in yaml.safe_load_all(k8s.read_text())
        if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "searxng-settings"
    )
    assert yaml.safe_load(configmap["data"]["settings.yml"]) == yaml.safe_load(
        (_DOCKER / "searxng-settings.yml").read_text()
    ), "the k8s ConfigMap and docker/searxng-settings.yml have drifted apart"
