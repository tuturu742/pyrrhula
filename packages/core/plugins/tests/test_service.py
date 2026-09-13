"""Plugin-repository service: structural validation, default-plugin sync, ownership
conflicts. Live Postgres (admin engine) via the same env the rest of the suite uses."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import uuid

import pytest
from sqlalchemy import select

from core.plugins.models import PluginRepositoryRow
from core.plugins.service import (
    PluginSyncError,
    baked_plugin_dir,
    ensure_default_synced,
    remove_repository,
    sync_repository,
    validate_plugin,
)
from core.tenancy.scope import admin_registry_session
from core.workflows.models import WorkflowRow

pytestmark = pytest.mark.asyncio


def test_validate_the_shipped_default_plugin() -> None:
    baked = baked_plugin_dir()
    assert baked is not None, "run scripts/fetch_plugins.py first"
    plugin, workflows = validate_plugin(baked)
    assert {w.key for w in workflows} == {"rpg", "swdev"}
    assert plugin.name


def test_validation_failures(tmp_path: pathlib.Path) -> None:
    with pytest.raises(PluginSyncError, match="plugin.json missing"):
        validate_plugin(tmp_path)

    (tmp_path / "plugin.json").write_text(json.dumps({"name": "x", "workflows": ["wf1"]}))
    with pytest.raises(PluginSyncError, match="workflow.json missing"):
        validate_plugin(tmp_path)

    wf = tmp_path / "wf1"
    wf.mkdir()
    (wf / "workflow.json").write_text(json.dumps({"key": "other", "name": "X"}))
    with pytest.raises(PluginSyncError, match="mismatched key"):
        validate_plugin(tmp_path)

    (wf / "workflow.json").write_text(json.dumps({"key": "wf1", "name": "X"}))
    with pytest.raises(PluginSyncError, match="no pack content"):
        validate_plugin(tmp_path)

    (wf / "schemas").mkdir()
    (wf / "schemas" / "broken.json").write_text("{nope")
    with pytest.raises(PluginSyncError, match="invalid JSON"):
        validate_plugin(tmp_path)


async def test_default_sync_registers_workflows(db_available: None) -> None:
    await ensure_default_synced()
    async with admin_registry_session() as session:
        row = await session.scalar(
            select(PluginRepositoryRow).where(PluginRepositoryRow.name == "default")
        )
        assert row is not None and row.status == "synced"
        assert set(row.workflow_keys) == {"rpg", "swdev"}
        keys = {
            w.key
            for w in (
                await session.execute(select(WorkflowRow).where(WorkflowRow.tenant_id.is_(None)))
            ).scalars()
        }
    assert {"rpg", "swdev", "default"} <= keys
    builtin = None
    async with admin_registry_session() as session:
        builtin = await session.scalar(
            select(PluginRepositoryRow).where(PluginRepositoryRow.name == "builtin")
        )
        assert builtin is not None and builtin.status == "synced"
        assert builtin.workflow_keys == ["default"]
    # Idempotent: a second call re-syncs nothing and changes nothing.
    await ensure_default_synced()


async def test_workflow_key_ownership_conflict(
    db_available: None, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.plugins.service as service

    monkeypatch.setattr(service, "plugins_root", lambda: tmp_path / "plugins-root")
    await ensure_default_synced()
    # A second repo claiming 'rpg' must fail its sync with a clear error. Real local
    # git repo, exactly the clone path production takes.
    baked = baked_plugin_dir()
    assert baked is not None
    source = tmp_path / "impostor"
    shutil.copytree(baked, source)
    (source / "plugin.json").write_text(json.dumps({"name": "impostor", "workflows": ["rpg"]}))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
    subprocess.run(["git", "add", "-A"], cwd=source, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
        cwd=source,
        check=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, capture_output=True, text=True, check=True
    ).stdout.strip()

    name = f"impostor-{uuid.uuid4().hex[:8]}"
    async with admin_registry_session() as session:
        row = PluginRepositoryRow(name=name, url=str(source), ref=sha, source="git")
        session.add(row)
        await session.flush()
        repo_id = row.id
    try:
        with pytest.raises(PluginSyncError, match="already provided"):
            await sync_repository(repo_id)
    finally:
        async with admin_registry_session() as session:
            fresh = await session.get(PluginRepositoryRow, repo_id)
            if fresh is not None:
                await session.delete(fresh)


async def test_baked_repo_cannot_be_removed(db_available: None) -> None:
    await ensure_default_synced()
    async with admin_registry_session() as session:
        row = await session.scalar(
            select(PluginRepositoryRow).where(PluginRepositoryRow.name == "default")
        )
        assert row is not None
        repo_id = row.id
    with pytest.raises(PluginSyncError, match="cannot be removed"):
        await remove_repository(repo_id)


# --- manual pack installation (no git, no credentials) --------------------------------
# The offline path: a deployment that cannot reach the pinned plugin repos must still be
# able to take packs by hand. These cover the archive handling itself, which is the part
# that touches the filesystem with operator-supplied names.


def _pack_bytes(kind: str, wrap: str = "") -> bytes:
    """A minimal valid pack as a .zip or .tar.gz, optionally inside a wrapping dir."""
    import io
    import tarfile
    import zipfile

    files = {
        "plugin.json": json.dumps({"name": "Sample", "workflows": ["wf1"]}),
        "wf1/workflow.json": json.dumps({"key": "wf1", "name": "WF One"}),
        "wf1/schemas/thing.json": json.dumps({"key": "thing", "definition": {"fields": []}}),
    }
    buf = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(buf, "w") as zf:
            for name, body in files.items():
                zf.writestr(wrap + name, body)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, body in files.items():
                data = body.encode()
                info = tarfile.TarInfo(wrap + name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _unpack(data: bytes, dest: pathlib.Path) -> pathlib.Path:
    from core.plugins.service import _unpack_into

    return _unpack_into(data, dest)


@pytest.mark.parametrize("kind", ["zip", "targz"])
@pytest.mark.parametrize("wrap", ["", "pyrrhula-workflows-main/"])
def test_unpack_accepts_both_formats_and_unwraps_a_single_root(
    tmp_path: pathlib.Path, kind: str, wrap: str
) -> None:
    from core.plugins.service import _unpack_into

    root = _unpack_into(_pack_bytes(kind, wrap), tmp_path / "staging")
    assert (root / "plugin.json").is_file()
    assert (root / "wf1" / "workflow.json").is_file()
    # The unwrapped content validates exactly as a cloned repository would.
    plugin, workflows = validate_plugin(root)
    assert plugin.name == "Sample"
    assert [w.key for w in workflows] == ["wf1"]


def test_unpack_refuses_entries_that_escape_the_pack(tmp_path: pathlib.Path) -> None:
    """An archive arrives over the network; a '..' member must never write outside."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("plugin.json", json.dumps({"name": "x", "workflows": ["wf1"]}))
        zf.writestr("../../escaped.json", "{}")
    with pytest.raises(PluginSyncError, match="escapes the pack"):
        _unpack(buf.getvalue(), tmp_path / "staging")
    assert not (tmp_path.parent / "escaped.json").exists()


def test_unpack_rejects_an_archive_with_no_plugin_manifest(tmp_path: pathlib.Path) -> None:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "not a pack")
    with pytest.raises(PluginSyncError, match="plugin.json not found"):
        _unpack(buf.getvalue(), tmp_path / "staging")


def test_unpack_rejects_junk(tmp_path: pathlib.Path) -> None:
    with pytest.raises(PluginSyncError, match="not a readable"):
        _unpack(b"this is not an archive", tmp_path / "staging")


def test_git_helpers_never_prompt_for_credentials() -> None:
    """An install (and an admin-console sync) must fail fast on a private repo rather
    than ask for a GitHub username -- the prompt hangs unattended installs and CI."""
    import importlib.util

    from core.plugins.service import _GIT_NONINTERACTIVE

    script = pathlib.Path(__file__).resolve().parents[4] / "scripts" / "fetch_plugins.py"
    spec = importlib.util.spec_from_file_location("_fetch_plugins", script)
    assert spec and spec.loader
    fetch_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fetch_mod)

    for env in (_GIT_NONINTERACTIVE, fetch_mod._GIT_NONINTERACTIVE):
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ASKPASS"]  # a helper that yields nothing, never a tty/GUI prompt
        assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
