"""What a preview actually runs, and where that answer comes from.

A preview used to be one thing: extract a tarball and serve it as a static site with
Python's stdlib server, which is right for a web build and useless for anything with a
process behind it. The build half of this pipeline was already configurable per repo
(``runtime_image``, ``setup_cmds``, ``test_cmd``, ``build_cmd``, ``artifact_name``) -- the
serving half was not, so a repo could build anything and then only ever preview a
directory of files.

Three layers answer it, most specific first:

1. **The repo row** -- an operator's override, editable in the UI. Wins because a broken
   recipe must be fixable without a commit and a redeploy.
2. **A manifest in the repo** (``pyrrhula-preview.json`` at the ref being previewed) --
   the recipe living with the code it describes, versioned alongside it, which is what
   makes this a property of the project rather than of somebody's workspace settings.
3. **The platform default** -- today's static server, so every existing repo keeps
   behaving exactly as it did.

**The platform keeps the parts that are not the user's business.** Fetching the artifact,
authenticating with a scoped read token, and extracting it with traversal guards stay in
``build_serve_command``'s preamble. A recipe chooses what runs *after* that, in the
extracted webroot. The alternative -- letting a manifest supply the whole container
command -- would hand the artifact token to arbitrary user code for no gain.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import Any

from core.ports.preview import PREVIEW_PORT

MANIFEST_PATH = "pyrrhula-preview.json"

# The static-site server this feature started as, and still the default: a build that
# produces an index.html needs no manifest and no configuration.
DEFAULT_SERVE_CMD = ""

_MAX_CMD_LEN = 2000
_MAX_ENV_VARS = 25
# Reserved: the preamble sets these, and a recipe that could overwrite them would be
# choosing its own artifact source -- i.e. reading a different tenant's build.
_RESERVED_ENV = frozenset({"PYR_ARTIFACT_URL", "PYR_ARTIFACT_TOKEN"})


class PreviewRecipeError(ValueError):
    """A manifest or override that cannot be honoured. Always names the field."""


@dataclass(frozen=True)
class PreviewRecipe:
    """Everything that varies between one preview and another."""

    image: str
    port: int = PREVIEW_PORT
    # Empty means the built-in static server.
    serve_cmd: str = DEFAULT_SERVE_CMD
    env: dict[str, str] = field(default_factory=dict)
    # Where each field came from, for the UI and for support questions.
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def is_default_static_site(self) -> bool:
        return not self.serve_cmd


def parse_manifest(text: str) -> dict[str, Any]:
    """The repo's own ``pyrrhula-preview.json``, validated.

    Raises rather than silently ignoring a malformed manifest: a preview that quietly
    falls back to serving static files when the author asked for a server is a worse
    outcome than a refusal that says which line is wrong.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PreviewRecipeError(f"{MANIFEST_PATH} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PreviewRecipeError(f"{MANIFEST_PATH} must be a JSON object")

    allowed = {"image", "port", "cmd", "env"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise PreviewRecipeError(
            f"{MANIFEST_PATH}: unknown field(s) {unknown}; allowed: {sorted(allowed)}"
        )
    return {k: v for k, v in data.items() if v not in (None, "")}


def _validated_cmd(value: Any, *, where: str) -> str:
    cmd = str(value).strip()
    if len(cmd) > _MAX_CMD_LEN:
        raise PreviewRecipeError(f"{where}: cmd is longer than {_MAX_CMD_LEN} characters")
    try:
        # Not used to execute -- the command runs under `sh -lc` so a pipeline is legal --
        # but an unbalanced quote here becomes an unreadable container crash later.
        shlex.split(cmd)
    except ValueError as exc:
        raise PreviewRecipeError(f"{where}: cmd is not parseable ({exc})") from exc
    return cmd


def _validated_port(value: Any, *, where: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise PreviewRecipeError(f"{where}: port must be a number") from exc
    if not (1 <= port <= 65535):
        raise PreviewRecipeError(f"{where}: port {port} is outside 1-65535")
    return port


def _validated_env(value: Any, *, where: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise PreviewRecipeError(f"{where}: env must be an object of name -> value")
    if len(value) > _MAX_ENV_VARS:
        raise PreviewRecipeError(f"{where}: more than {_MAX_ENV_VARS} env vars")
    out: dict[str, str] = {}
    for name, raw in value.items():
        key = str(name)
        if key in _RESERVED_ENV:
            raise PreviewRecipeError(
                f"{where}: {key} is set by the platform and cannot be overridden -- it is "
                "how the container reaches its own artifact"
            )
        out[key] = str(raw)
    return out


def resolve_recipe(
    *,
    default_image: str,
    repo_overrides: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
) -> PreviewRecipe:
    """Fold the three layers into one answer, recording where each field came from."""
    overrides = {k: v for k, v in (repo_overrides or {}).items() if v not in (None, "", {})}
    declared = manifest or {}
    sources: dict[str, str] = {}

    def pick(field_name: str, fallback: Any) -> tuple[Any, str]:
        if field_name in overrides:
            return overrides[field_name], "repo settings"
        if field_name in declared:
            return declared[field_name], MANIFEST_PATH
        return fallback, "platform default"

    image_value, sources["image"] = pick("image", default_image)
    port_value, sources["port"] = pick("port", PREVIEW_PORT)
    cmd_value, sources["cmd"] = pick("cmd", DEFAULT_SERVE_CMD)
    env_value, sources["env"] = pick("env", {})

    image = str(image_value).strip()
    if not image:
        raise PreviewRecipeError("image: no preview image configured and no default set")

    return PreviewRecipe(
        image=image,
        port=_validated_port(port_value, where=sources["port"]),
        serve_cmd=_validated_cmd(cmd_value, where=sources["cmd"]) if cmd_value else "",
        env=_validated_env(env_value, where=sources["env"]) if env_value else {},
        sources=sources,
    )


async def read_repo_manifest(store_key: str, *, ref: str = "main") -> dict[str, Any]:
    """``pyrrhula-preview.json`` at ``ref``, or ``{}`` when the repo does not ship one.

    Read server-side from the git store rather than out of the built artifact, because the
    image and port have to be known *before* a container exists to extract anything in.
    A missing manifest is the normal case and says nothing; a malformed one raises, so an
    author who meant to configure a preview is told rather than silently given the static
    server.
    """
    from adapters.mcp.git_store import GitStore, GitStoreError, default_git_root

    store = GitStore(default_git_root())
    try:
        text = await store._git(store_key, "show", f"{ref}:{MANIFEST_PATH}")
    except GitStoreError:
        return {}
    except Exception:  # noqa: BLE001 -- a missing repo or ref is "no manifest", not a 500
        return {}
    return parse_manifest(text)
