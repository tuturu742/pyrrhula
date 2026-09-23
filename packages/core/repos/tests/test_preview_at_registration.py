"""A repo can be registered with its preview recipe, not only its build recipe.

Registration accepted `runtime_image`, `setup_cmds`, `test_cmd`, `build_cmd` and
`artifact_name` and stopped there, so a scripted setup -- a sample's `repos.json`, an
operator's script -- could say exactly how a project is built and nothing at all about
how it is served. The preview then fell through to the platform's static-site default,
which for a project with a process behind it serves a directory listing and looks like a
broken build rather than an unfinished registration.
"""

from __future__ import annotations

import uuid

import pytest

from core.repos.service import InvalidRepoError, create_repo, get_repo
from core.tenancy.seed import seed_dev_tenant


async def _tenant() -> uuid.UUID:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"preview-reg-{uuid.uuid4().hex[:8]}"
    )
    return tenant_id


async def test_a_preview_recipe_survives_registration(db_available: None) -> None:
    tenant_id = await _tenant()
    row = await create_repo(
        tenant_id,
        "tui-app",
        "TUI app",
        preview_image="localhost:5000/tui-preview:1",
        preview_cmd="ttyd -p 8080 -W ./app",
        preview_port=8080,
        preview_env={"TERM": "xterm-256color"},
    )
    stored = await get_repo(tenant_id, row.id)
    assert stored is not None
    assert stored.preview_image == "localhost:5000/tui-preview:1"
    assert stored.preview_cmd == "ttyd -p 8080 -W ./app"
    assert stored.preview_port == 8080
    assert stored.preview_env == {"TERM": "xterm-256color"}


async def test_no_preview_recipe_stays_the_platform_default(db_available: None) -> None:
    """Unset must stay unset: an empty string stored as an override would beat the repo's
    own `pyrrhula-preview.json`, which is the layer meant to win when nobody overrode it."""
    tenant_id = await _tenant()
    row = await create_repo(tenant_id, "plain", "Plain", preview_image="  ", preview_cmd="")
    stored = await get_repo(tenant_id, row.id)
    assert stored is not None
    assert stored.preview_image is None
    assert stored.preview_cmd is None
    assert stored.preview_port is None
    assert stored.preview_env == {}


async def test_a_bad_port_is_refused_at_registration(db_available: None) -> None:
    """Not at preview time, inside a container nobody is watching."""
    tenant_id = await _tenant()
    with pytest.raises(InvalidRepoError):
        await create_repo(tenant_id, "bad-port", "Bad port", preview_port=99999)


async def test_the_artifact_env_cannot_be_overridden(db_available: None) -> None:
    """`PYR_ARTIFACT_URL`/`_TOKEN` are how the container reaches its own build; a recipe
    that could set them would be choosing which build it previews."""
    tenant_id = await _tenant()
    with pytest.raises(InvalidRepoError):
        await create_repo(
            tenant_id,
            "sneaky",
            "Sneaky",
            preview_env={"PYR_ARTIFACT_URL": "http://elsewhere/artifact.tgz"},
        )
