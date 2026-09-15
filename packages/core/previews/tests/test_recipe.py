"""What a preview runs, and who gets to decide it."""

from __future__ import annotations

import base64
import re

import pytest

from core.ports.preview import PREVIEW_PORT
from core.previews.recipe import (
    MANIFEST_PATH,
    PreviewRecipeError,
    parse_manifest,
    resolve_recipe,
)
from core.previews.service import build_serve_command

DEFAULT_IMAGE = "docker.io/library/python:3.12-slim"


def _program(command: str) -> str:
    return base64.b64decode(re.search(r"b64decode\('([^']+)'\)", command).group(1)).decode()


def test_a_repo_with_no_recipe_still_gets_the_static_site_server() -> None:
    """The feature this replaces is the default, so every existing repo is untouched."""
    recipe = resolve_recipe(default_image=DEFAULT_IMAGE)

    assert recipe.image == DEFAULT_IMAGE
    assert recipe.port == PREVIEW_PORT
    assert recipe.is_default_static_site
    program = _program(build_serve_command(port=recipe.port, serve_cmd=recipe.serve_cmd))
    assert "no index.html in artifact" in program, "the static server is gone"
    assert "serve_cmd = ''" in program


def test_a_manifest_in_the_repo_chooses_what_runs() -> None:
    manifest = parse_manifest('{"image": "node:22-slim", "cmd": "node server.js", "port": 3000}')
    recipe = resolve_recipe(default_image=DEFAULT_IMAGE, manifest=manifest)

    assert recipe.image == "node:22-slim"
    assert recipe.serve_cmd == "node server.js"
    assert recipe.port == 3000
    assert recipe.sources["cmd"] == MANIFEST_PATH
    program = _program(build_serve_command(port=recipe.port, serve_cmd=recipe.serve_cmd))
    assert "os.execv" in program and "node server.js" in program


def test_repo_settings_override_the_manifest() -> None:
    """An operator has to be able to fix a broken recipe without a commit and a rebuild."""
    manifest = parse_manifest('{"image": "node:22-slim", "cmd": "node server.js"}')
    recipe = resolve_recipe(
        default_image=DEFAULT_IMAGE,
        repo_overrides={"cmd": "node server.js --port 8080"},
        manifest=manifest,
    )

    assert recipe.serve_cmd == "node server.js --port 8080"
    assert recipe.sources["cmd"] == "repo settings"
    assert recipe.image == "node:22-slim", "an unset override must not clobber the manifest"
    assert recipe.sources["image"] == MANIFEST_PATH


def test_the_platform_keeps_the_artifact_fetch_whatever_the_recipe_says() -> None:
    """A recipe replaces the serving half only. Fetching the artifact with a scoped token
    and extracting it under PEP 706's guard is where the credential and the write path
    are, and no repo-supplied configuration reaches either."""
    recipe = resolve_recipe(
        default_image=DEFAULT_IMAGE, manifest=parse_manifest('{"cmd": "./run.sh"}')
    )
    program = _program(build_serve_command(port=recipe.port, serve_cmd=recipe.serve_cmd))

    assert 'os.environ["PYR_ARTIFACT_TOKEN"]' in program
    assert 'filter="data"' in program
    assert program.index("tar.extractall") < program.index("os.execv"), (
        "the recipe must run after extraction, not instead of it"
    )


def test_a_recipe_cannot_reaim_the_container_at_another_artifact() -> None:
    for reserved in ("PYR_ARTIFACT_URL", "PYR_ARTIFACT_TOKEN"):
        with pytest.raises(PreviewRecipeError, match=reserved):
            resolve_recipe(
                default_image=DEFAULT_IMAGE,
                manifest={"env": {reserved: "http://elsewhere.invalid"}},
            )


def test_a_malformed_manifest_is_refused_rather_than_ignored() -> None:
    """Falling back to static files would hand an author a directory listing with no hint
    that the file they wrote was skipped."""
    with pytest.raises(PreviewRecipeError, match="not valid JSON"):
        parse_manifest("{not json")
    with pytest.raises(PreviewRecipeError, match="unknown field"):
        parse_manifest('{"command": "node server.js"}')
    with pytest.raises(PreviewRecipeError, match="port"):
        resolve_recipe(default_image=DEFAULT_IMAGE, manifest={"port": 99999})
    with pytest.raises(PreviewRecipeError, match="cmd"):
        resolve_recipe(default_image=DEFAULT_IMAGE, manifest={"cmd": 'node "unclosed'})


def test_an_absent_manifest_is_silence_not_an_error() -> None:
    """Most repos will never ship one."""
    assert parse_manifest("{}") == {}
    assert resolve_recipe(default_image=DEFAULT_IMAGE, manifest={}).is_default_static_site
