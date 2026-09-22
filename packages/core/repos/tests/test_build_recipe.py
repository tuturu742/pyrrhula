"""Where a build's instructions come from.

The serving half of this pipeline let a repo describe itself in a manifest versioned with
the code; the building half kept the same four fields in a database row. So a test
command could not differ between branches, could not be reviewed in the pull request that
changed it, and did not travel with a fork.
"""

from __future__ import annotations

import pytest

from core.repos.build_recipe import (
    MANIFEST_PATH,
    BuildRecipeError,
    parse_manifest,
    resolve_build_recipe,
)

_RUNTIME = {"image": "docker.io/library/debian:bookworm", "setup": ["apt-get update"]}


def _resolve(**overrides):  # noqa: ANN003, ANN202
    args = {
        "repo_runtime": "debian",
        "repo_image": None,
        "repo_setup_cmds": [],
        "repo_test_cmd": None,
        "repo_build_cmd": None,
        "repo_artifact_name": None,
        "manifest": {},
        "runtime_entry": _RUNTIME,
    }
    args.update(overrides)
    return resolve_build_recipe(**args)


def test_a_repo_with_no_manifest_behaves_exactly_as_before() -> None:
    recipe = _resolve(repo_test_cmd="pytest", repo_setup_cmds=["pip install -e ."])
    assert recipe.image == _RUNTIME["image"]
    assert recipe.test_cmd == "pytest"
    # The runtime's baseline setup runs first: it is what makes the image usable, and the
    # repo's own commands assume it has.
    assert recipe.setup_cmds == ["apt-get update", "pip install -e ."]


def test_a_manifest_supplies_what_the_row_leaves_unset() -> None:
    recipe = _resolve(
        manifest={
            "test_cmd": "cargo test",
            "build_cmd": "cargo build",
            "artifact_name": "app.tar.gz",
        }
    )
    assert recipe.test_cmd == "cargo test"
    assert recipe.sources["test_cmd"] == "manifest"


def test_the_row_wins_field_by_field_not_wholesale() -> None:
    """An operator fixing one broken command must not silently discard the other three.
    Taking the whole recipe from whichever layer spoke first would do exactly that."""
    recipe = _resolve(
        repo_test_cmd="cargo test --lib",
        manifest={
            "test_cmd": "cargo test",
            "build_cmd": "cargo build",
            "artifact_name": "app.tar.gz",
        },
    )
    assert recipe.test_cmd == "cargo test --lib"
    assert recipe.sources["test_cmd"] == "repo"
    assert recipe.build_cmd == "cargo build"
    assert recipe.sources["build_cmd"] == "manifest"


def test_an_operators_image_cannot_be_moved_by_a_manifest() -> None:
    """A repo pinned to an explicit image meant that image. A manifest that could
    redirect the build onto another one would make the override advisory."""
    recipe = _resolve(
        repo_runtime="custom",
        repo_image="ghcr.io/acme/builder:1",
        runtime_entry=None,
        manifest={"image": "docker.io/library/alpine"},
    )
    assert recipe.image == "ghcr.io/acme/builder:1"
    assert recipe.sources["image"] == "repo"


def test_a_manifest_may_name_a_runtime_and_gets_its_baseline_setup() -> None:
    rust = {"image": "docker.io/library/rust:1.97", "setup": ["rustup component add clippy"]}
    recipe = _resolve(
        runtime_entry=None,
        repo_runtime="nope",
        manifest={"runtime": "rust"},
        manifest_runtime_entry=rust,
    )
    assert recipe.image == rust["image"]
    assert recipe.setup_cmds == ["rustup component add clippy"]


def test_a_build_with_nowhere_to_put_its_output_is_refused() -> None:
    """It would run, produce a file nobody collects, and read as a build that silently
    did not happen."""
    with pytest.raises(BuildRecipeError, match="artifact_name"):
        _resolve(manifest={"build_cmd": "make"})


def test_no_image_anywhere_is_an_error_not_a_default() -> None:
    with pytest.raises(BuildRecipeError, match="no image"):
        _resolve(repo_runtime="typo", runtime_entry=None)


def test_a_malformed_manifest_raises_rather_than_being_ignored() -> None:
    """An author who wrote one meant it. Falling back to the row would leave them
    debugging a build that never read their file."""
    with pytest.raises(BuildRecipeError, match="not valid JSON"):
        parse_manifest("{oops")
    with pytest.raises(BuildRecipeError, match="JSON object"):
        parse_manifest("[1, 2]")


def test_an_absent_manifest_is_silent() -> None:
    assert parse_manifest("") == {}
    assert parse_manifest("   \n") == {}


def test_an_artifact_name_is_a_file_not_a_path() -> None:
    """The platform uploads whatever sits at this name after the build. A path would let
    a manifest choose which file gets read."""
    for bad in ("../secrets", "dist/app.tar.gz", ".env"):
        with pytest.raises(BuildRecipeError, match="bare file name"):
            parse_manifest(f'{{"artifact_name": "{bad}"}}')


def test_the_manifest_is_named_for_the_platform_like_its_preview_sibling() -> None:
    assert MANIFEST_PATH == "pyrrhula-build.json"
