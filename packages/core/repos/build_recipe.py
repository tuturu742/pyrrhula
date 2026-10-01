"""What a delegation builds in a repo, and where that answer comes from.

The serving half of this pipeline already worked this way (``core.previews.recipe``): a
repo describes its own preview in ``pyrrhula-preview.json``, versioned with the code,
overridable by an operator. The building half did not. ``setup_cmds``, ``test_cmd``,
``build_cmd`` and ``artifact_name`` lived only in a database row, which meant the build
could not differ between branches, could not be reviewed in the pull request that changed
it, and did not travel with a fork. A repo whose test command changed had to have someone
edit a workspace setting to match.

The same three layers answer it now, most specific first:

1. **The repo row** -- an operator's override, editable in the UI. Wins because a broken
   build must be fixable without a commit and a redeploy.
2. **A manifest in the repo** (``pyrrhula-build.json`` at the ref being worked) -- the
   recipe living with the code it describes.
3. **The runtime catalog** -- the image and baseline setup for the named runtime, which
   a tenant can now register itself (``core.repos.runtimes``).

**Per field, not per layer.** A repo row that overrides only ``test_cmd`` leaves the
manifest's ``build_cmd`` in force. Taking the whole recipe from whichever layer spoke
first would mean an operator fixing one command silently discarded the other three.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

MANIFEST_PATH = "pyrrhula-build.json"

_MAX_CMD_LEN = 511
_MAX_SETUP_CMDS = 20
_MAX_NAME_LEN = 255


class BuildRecipeError(ValueError):
    """A manifest or override that cannot be honoured. Always names the field."""


@dataclass(frozen=True)
class BuildRecipe:
    """Everything a delegation needs to build and test one repo."""

    image: str
    setup_cmds: list[str] = field(default_factory=list)
    test_cmd: str | None = None
    build_cmd: str | None = None
    artifact_name: str | None = None
    # Which layer supplied each field, for the UI and for a note in the transcript: a
    # build that ran something unexpected should be traceable to where it was written.
    sources: dict[str, str] = field(default_factory=dict)


def _clean_cmd(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > _MAX_CMD_LEN:
        raise BuildRecipeError(f"{field_name} is longer than {_MAX_CMD_LEN} characters")
    return text


def parse_manifest(text: str) -> dict[str, Any]:
    """``pyrrhula-build.json``'s contents, validated. ``{}`` for an empty file."""
    if not text.strip():
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BuildRecipeError(f"{MANIFEST_PATH} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise BuildRecipeError(f"{MANIFEST_PATH} must contain a JSON object")

    out: dict[str, Any] = {}
    if "runtime" in raw:
        out["runtime"] = str(raw["runtime"]).strip()
    if "image" in raw:
        # Through the same validator as a typed image. This file lives in the repository,
        # so anyone who can commit to it chooses the image -- and, before this, it was only
        # checked for spaces, while the repo's registry credential followed it anywhere.
        from core.repos.image_ref import ImageRefError, normalise_image_ref

        try:
            image = normalise_image_ref(str(raw["image"]))
        except ImageRefError as exc:
            raise BuildRecipeError(f"image: {exc}") from exc
        if image:
            out["image"] = image
    for key in ("test_cmd", "build_cmd"):
        if key in raw:
            out[key] = _clean_cmd(raw[key], key)
    if "artifact_name" in raw:
        name = str(raw["artifact_name"]).strip()
        if len(name) > _MAX_NAME_LEN:
            raise BuildRecipeError(f"artifact_name is longer than {_MAX_NAME_LEN} characters")
        if "/" in name or name.startswith("."):
            # The build leaves this file in the working directory and the platform
            # uploads it by name. A path would let a manifest choose what gets read.
            raise BuildRecipeError("artifact_name must be a bare file name")
        out["artifact_name"] = name
    if "setup_cmds" in raw:
        raw_setup = raw["setup_cmds"]
        if not isinstance(raw_setup, list):
            raise BuildRecipeError("setup_cmds must be a list of strings")
        if len(raw_setup) > _MAX_SETUP_CMDS:
            raise BuildRecipeError(f"at most {_MAX_SETUP_CMDS} setup commands")
        cleaned = [_clean_cmd(c, "setup_cmds") for c in raw_setup]
        out["setup_cmds"] = [c for c in cleaned if c]
    return out


async def read_repo_manifest(store_key: str, *, ref: str = "main") -> dict[str, Any]:
    """``pyrrhula-build.json`` at ``ref``, or ``{}`` when the repo does not ship one.

    Read from the git store rather than from a checkout, because the image has to be
    known before there is a container to check anything out in. A missing manifest is the
    normal case and says nothing; a malformed one raises, so an author who meant to
    configure a build is told rather than silently given the old defaults.
    """
    from adapters.mcp.git_store import GitStore, GitStoreError, default_git_root

    store = GitStore(default_git_root())
    try:
        text = await store._git(store_key, "show", f"{ref}:{MANIFEST_PATH}")  # noqa: SLF001
    except GitStoreError:
        return {}
    except Exception:  # noqa: BLE001 -- a missing repo or ref is "no manifest", not a 500
        return {}
    return parse_manifest(text)


def resolve_build_recipe(
    *,
    repo_runtime: str | None,
    repo_image: str | None,
    repo_setup_cmds: list[str] | None,
    repo_test_cmd: str | None,
    repo_build_cmd: str | None,
    repo_artifact_name: str | None,
    manifest: dict[str, Any],
    runtime_entry: dict[str, object] | None,
    manifest_runtime_entry: dict[str, object] | None = None,
) -> BuildRecipe:
    """Fold the three layers into one recipe, field by field.

    ``runtime_entry`` is the catalog entry for the repo row's runtime;
    ``manifest_runtime_entry`` the one for a runtime the manifest named. A repo pinned to
    ``custom`` with an explicit image keeps it: an operator who named an image meant that
    image, and a manifest must not be able to move the build onto a different one.
    """
    sources: dict[str, str] = {}

    # Image. A repo-row image is the operator's word and wins outright. Otherwise the
    # manifest may name an image or a runtime; otherwise the repo row's runtime.
    baseline: list[str] = []
    if repo_image:
        image = repo_image
        sources["image"] = "repo"
    elif manifest.get("image"):
        image = str(manifest["image"])
        sources["image"] = "manifest"
    elif manifest_runtime_entry is not None:
        image = str(manifest_runtime_entry["image"])
        raw = manifest_runtime_entry.get("setup")
        baseline = [str(c) for c in raw] if isinstance(raw, list) else []
        sources["image"] = "manifest-runtime"
    elif runtime_entry is not None:
        image = str(runtime_entry["image"])
        raw = runtime_entry.get("setup")
        baseline = [str(c) for c in raw] if isinstance(raw, list) else []
        sources["image"] = "runtime"
    else:
        raise BuildRecipeError(
            f"no image for this build: repo runtime {repo_runtime!r} is not a known "
            "runtime and neither the repo nor its manifest names an image"
        )

    def pick(field_name: str, repo_value: Any) -> Any:
        if repo_value not in (None, "", []):
            sources[field_name] = "repo"
            return repo_value
        if manifest.get(field_name) not in (None, "", []):
            sources[field_name] = "manifest"
            return manifest[field_name]
        return None

    setup = pick("setup_cmds", list(repo_setup_cmds or [])) or []
    test_cmd = pick("test_cmd", repo_test_cmd)
    build_cmd = pick("build_cmd", repo_build_cmd)
    artifact_name = pick("artifact_name", repo_artifact_name)

    # A build command with nowhere to put its output uploads nothing, which reads as a
    # build that silently did not happen. Say so at resolution instead.
    if build_cmd and not artifact_name:
        raise BuildRecipeError(
            "build_cmd is set but artifact_name is not, so nothing would be collected "
            "after the build runs"
        )

    return BuildRecipe(
        image=image,
        # The runtime's baseline setup runs first: it is what makes the image usable
        # (git, ca-certificates), and a repo's own commands assume it has.
        setup_cmds=[*baseline, *setup],
        test_cmd=test_cmd,
        build_cmd=build_cmd,
        artifact_name=artifact_name,
        sources=sources,
    )
