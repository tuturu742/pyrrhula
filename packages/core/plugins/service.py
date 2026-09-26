"""Plugin-repository sync: registered git repositories become system workflows.

Sync is an operator action running with the admin engine (``admin_registry_session``)
because it writes NULL-tenant rows (``workflow`` templates, global
``vocabulary_overlay``) that the app role's RLS WITH CHECK structurally forbids --
the same reason the admin console owns tenant provisioning. Content is validated
declaratively (pydantic manifests; the pack loader only ever reads JSON) and nothing
from a plugin executes, honoring CLAUDE.md rules 9 and 10.

The **default** plugin's pinned content ships inside the image (``/app/packs``,
copied from ``.plugins/default`` at build -- see deploy/plugins.json), so first boot
syncs without the network. Added repositories are shallow-cloned at their pinned ref
into ``<blob_root>/plugins/<name>``.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import uuid
import zipfile
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from core.config import get_settings
from core.plugins.models import PluginManifest, PluginRepositoryRow, WorkflowManifest
from core.tenancy.scope import admin_registry_session, unscoped_session
from core.vocabulary.models import VocabularyOverlayRow
from core.workflows.models import WorkflowRow

_PACK_SUBDIRS = ("schemas", "processes", "rule_systems", "tools", "axes", "overlay", "seed")


class PluginSyncError(Exception):
    pass


def plugins_root() -> pathlib.Path:
    return pathlib.Path(get_settings().blob_store_root) / "plugins"


def baked_plugin_dir() -> pathlib.Path | None:
    """The default plugin's in-image content; repo-checkout fallback for local dev."""
    for candidate in (
        pathlib.Path("/app/packs"),
        pathlib.Path(__file__).resolve().parents[3] / ".plugins" / "default",
    ):
        if (candidate / "plugin.json").is_file():
            return candidate
    return None


def builtin_plugin_dir() -> pathlib.Path | None:
    """The platform's own built-in workflows (ship in-tree; a fresh install has a
    working 'default' workflow before any plugin repository exists)."""
    for candidate in (
        pathlib.Path("/app/builtin-workflows"),
        pathlib.Path(__file__).resolve().parents[3] / "builtin-workflows",
    ):
        if (candidate / "plugin.json").is_file():
            return candidate
    return None


def plugin_drop_dir() -> pathlib.Path:
    """Where an operator hand-places packs when this deployment cannot reach git.

    Mount a host directory (or a ConfigMap) at ``PYRRHULA_PLUGIN_DROP_DIR`` and every
    subdirectory containing a ``plugin.json`` is registered and synced at boot. The files
    stay the operator's -- the platform only reads them, so a drop-in is removed by
    deleting it from this directory, not through the console.
    """
    return pathlib.Path(get_settings().plugin_drop_dir)


# Reserved for the two rows ensure_default_synced owns; a hand-placed or uploaded pack
# must not shadow them.
_SYSTEM_NAMES = ("builtin", "default")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")


def content_fingerprint(content: pathlib.Path) -> str:
    """Stable hash of a plugin directory's JSON content -- the 'ref' for in-image
    sources, so an image upgrade with changed content triggers a re-sync."""
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(content.rglob("*.json")):
        digest.update(str(path.relative_to(content)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _content_dir(row: PluginRepositoryRow) -> pathlib.Path:
    if row.source in ("baked", "builtin"):
        content = baked_plugin_dir() if row.source == "baked" else builtin_plugin_dir()
        if content is None:
            raise PluginSyncError(f"{row.source} plugin content missing from this deployment")
        return content
    if row.source == "local":
        content = plugin_drop_dir() / row.name
        if not (content / "plugin.json").is_file():
            raise PluginSyncError(f"dropped plugin {row.name!r} is no longer present at {content}")
        return content
    return plugins_root() / row.name


# An operator syncing a repository from the admin console gets the same guarantee the
# installer does: git never prompts. Without this a private URL would park a worker thread
# on a credential prompt until the timeout, and report a timeout instead of "auth failed".
_GIT_NONINTERACTIVE = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/true",
    "SSH_ASKPASS": "/bin/true",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
}


def _clone(row: PluginRepositoryRow) -> pathlib.Path:
    dest = plugins_root() / row.name
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **_GIT_NONINTERACTIVE}
    try:
        subprocess.run(
            ["git", "clone", "--quiet", row.url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        subprocess.run(
            ["git", "checkout", "--quiet", row.ref],
            cwd=dest,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        raise PluginSyncError(f"git failed: {(exc.stderr or str(exc))[:300]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PluginSyncError(f"git timed out: {exc}") from exc
    shutil.rmtree(dest / ".git", ignore_errors=True)
    return dest


def validate_plugin(content: pathlib.Path) -> tuple[PluginManifest, list[WorkflowManifest]]:
    """Structural validation only -- deep content validation happens through the same
    authoring models the pack loader uses when a tenant selects the workflow."""
    manifest_path = content / "plugin.json"
    if not manifest_path.is_file():
        raise PluginSyncError("plugin.json missing at repository root")
    try:
        plugin = PluginManifest.model_validate(json.loads(manifest_path.read_text()))
    except (ValidationError, json.JSONDecodeError) as exc:
        raise PluginSyncError(f"invalid plugin.json: {str(exc)[:300]}") from exc

    workflows: list[WorkflowManifest] = []
    for key in plugin.workflows:
        wf_dir = content / key
        wf_manifest = wf_dir / "workflow.json"
        if not wf_manifest.is_file():
            raise PluginSyncError(f"workflow {key!r}: {key}/workflow.json missing")
        try:
            manifest = WorkflowManifest.model_validate(json.loads(wf_manifest.read_text()))
        except (ValidationError, json.JSONDecodeError) as exc:
            raise PluginSyncError(
                f"workflow {key!r}: invalid workflow.json: {str(exc)[:300]}"
            ) from exc
        if manifest.key != key:
            raise PluginSyncError(f"workflow dir {key!r} declares mismatched key {manifest.key!r}")
        if not any((wf_dir / sub).is_dir() for sub in _PACK_SUBDIRS):
            raise PluginSyncError(f"workflow {key!r}: no pack content directories found")
        # Every JSON file must parse -- a plugin is data, and broken data fails loudly
        # at sync, not at tenant selection.
        for path in wf_dir.rglob("*.json"):
            try:
                json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                raise PluginSyncError(f"{path.relative_to(content)}: invalid JSON: {exc}") from exc
        _deep_validate_workflow(content, key)
        workflows.append(manifest)
    return plugin, workflows


def _deep_validate_workflow(content: pathlib.Path, key: str) -> None:
    """Dry-run every content kind through the SAME validation models the pack loader
    uses -- authors get content errors at sync time, not when a tenant selects the
    workflow. Purely in-memory; nothing is written."""
    from core.behavior.validation import AxisDefinitionSchema
    from core.entities.schema import EntitySchemaDefinition
    from core.process.dsl.validator import validate_raw
    from core.resolution.registry import ToolDefinitionSchema
    from core.resolution.rule_system import RuleSystemDefinitionSchema

    wf_dir = content / key

    def _each(subdir: str) -> list[tuple[pathlib.Path, dict[str, Any]]]:
        d = wf_dir / subdir
        if not d.is_dir():
            return []
        return [(p, json.loads(p.read_text())) for p in sorted(d.rglob("*.json"))]

    try:
        for _path, data in _each("schemas"):
            EntitySchemaDefinition.model_validate(data.get("definition", data))
        for path, data in _each("processes"):
            # Pack process files wrap the DSL: {key, name, definition} (see loader).
            _, issues = validate_raw(data.get("definition", data))
            if issues:
                raise PluginSyncError(
                    f"{path.relative_to(content)}: {'; '.join(str(i) for i in issues[:3])}"
                )
        for _path, data in _each("tools"):
            ToolDefinitionSchema.model_validate(data)
        for _path, data in _each("axes"):
            AxisDefinitionSchema.model_validate(data)
        for _path, data in _each("rule_systems"):
            RuleSystemDefinitionSchema.model_validate(data)
    except PluginSyncError:
        raise
    except Exception as exc:  # noqa: BLE001 -- name the file, whatever the model raised
        raise PluginSyncError(
            f"workflow {key!r}: {path.relative_to(content)}: {str(exc)[:300]}"
        ) from exc


async def _upsert_globals(
    repo_id: uuid.UUID, content: pathlib.Path, workflows: list[WorkflowManifest]
) -> None:
    async with admin_registry_session() as session:
        # Cross-repo key ownership: a workflow key belongs to exactly one repository.
        rows = (await session.execute(select(PluginRepositoryRow))).scalars().all()
        owned_elsewhere = {
            key: row.name for row in rows if row.id != repo_id for key in row.workflow_keys
        }
        for manifest in workflows:
            if manifest.key in owned_elsewhere:
                raise PluginSyncError(
                    f"workflow key {manifest.key!r} is already provided by plugin "
                    f"repository {owned_elsewhere[manifest.key]!r}"
                )

        for manifest in workflows:
            # Global overlays from the workflow's overlay/ directory.
            for overlay_path in sorted((content / manifest.key / "overlay").glob("*.json")):
                data = json.loads(overlay_path.read_text())
                overlay = await session.scalar(
                    select(VocabularyOverlayRow).where(
                        VocabularyOverlayRow.tenant_id.is_(None),
                        VocabularyOverlayRow.key == str(data["key"]),
                    )
                )
                if overlay is None:
                    overlay = VocabularyOverlayRow(
                        tenant_id=None,
                        key=str(data["key"]),
                        name=str(data.get("name") or data["key"]),
                    )
                    session.add(overlay)
                overlay.labels = dict(data.get("labels") or {})
                # The name updates too: without this, a rename in the pack never lands
                # and whatever name the row was first created under sticks forever --
                # which is how default_v1 stayed "Enterprise Workflow" while the
                # workflow picker said "Default", confusing every setup guide.
                if data.get("name"):
                    overlay.name = str(data["name"])

            template = await session.scalar(
                select(WorkflowRow).where(
                    WorkflowRow.tenant_id.is_(None), WorkflowRow.key == manifest.key
                )
            )
            if template is None:
                template = WorkflowRow(tenant_id=None, key=manifest.key, name=manifest.name)
                session.add(template)
            template.name = manifest.name
            template.overlay_key = manifest.overlay_key
            template.persona_type_labels = manifest.persona_type_labels
            template.label_overrides = manifest.label_overrides
            template.featured_process_keys = manifest.featured_process_keys
            template.capabilities = manifest.capabilities


async def sync_repository(repository_id: uuid.UUID) -> PluginRepositoryRow:
    async with admin_registry_session() as session:
        row = await session.get(PluginRepositoryRow, repository_id)
        if row is None:
            raise PluginSyncError(f"no plugin repository {repository_id}")
        session.expunge(row)

    try:
        # Only a git repository is fetched; every other source ('baked', 'builtin',
        # 'local', 'upload') already has its content on disk.
        content = await asyncio.to_thread(_clone, row) if row.source == "git" else _content_dir(row)
        _, workflows = validate_plugin(content)
        await _upsert_globals(row.id, content, workflows)
        status, error, keys = "synced", "", [w.key for w in workflows]
    except PluginSyncError as exc:
        status, error, keys = "error", str(exc), list(row.workflow_keys)

    async with admin_registry_session() as session:
        fresh = await session.get(PluginRepositoryRow, repository_id)
        assert fresh is not None
        fresh.status = status
        fresh.last_error = error
        fresh.workflow_keys = keys
        fresh.last_synced_at = datetime.now(UTC)
        await session.flush()
        session.expunge(fresh)
    if status == "error":
        raise PluginSyncError(error)
    return fresh


async def list_repositories() -> list[PluginRepositoryRow]:
    """App-role read (SELECT is granted; writes are not) -- usable from api/worker for
    workflow pack lookup, not only from the admin console."""
    async with unscoped_session() as session:
        query = select(PluginRepositoryRow).order_by(PluginRepositoryRow.created_at)
        rows = (await session.execute(query)).scalars().all()
        for row in rows:
            session.expunge(row)
        return list(rows)


async def add_repository(name: str, url: str, ref: str) -> PluginRepositoryRow:
    async with admin_registry_session() as session:
        row = PluginRepositoryRow(name=name, url=url, ref=ref, source="git")
        session.add(row)
        await session.flush()
        session.expunge(row)
    return await sync_repository(row.id)


async def remove_repository(repository_id: uuid.UUID) -> None:
    """Refuses while any tenant's selected workflow comes from this repository."""
    from core.tenancy.models import Tenant

    async with admin_registry_session() as session:
        row = await session.get(PluginRepositoryRow, repository_id)
        if row is None:
            return
        if row.source in ("baked", "builtin"):
            raise PluginSyncError("system plugin repositories cannot be removed")
        if row.source == "local":
            # Deleting the row would achieve nothing: boot rediscovers the directory.
            raise PluginSyncError(
                f"{row.name!r} comes from the plugin drop directory -- delete it from "
                f"{plugin_drop_dir()} and restart; removing it here would not stick"
            )
        selected = {
            (t.settings or {}).get("workflow_key")
            for t in (await session.execute(select(Tenant))).scalars()
        }
        in_use = sorted(set(row.workflow_keys) & selected)
        if in_use:
            raise PluginSyncError(
                f"workflows in use by tenants: {', '.join(in_use)} -- switch those tenants first"
            )
        for key in row.workflow_keys:
            template = await session.scalar(
                select(WorkflowRow).where(WorkflowRow.tenant_id.is_(None), WorkflowRow.key == key)
            )
            if template is not None:
                await session.delete(template)
        await session.delete(row)
    shutil.rmtree(plugins_root() / row.name, ignore_errors=True)


def pack_dir_for_workflow(key: str, repositories: list[PluginRepositoryRow]) -> pathlib.Path | None:
    for row in repositories:
        if key in row.workflow_keys:
            return _content_dir(row) / key
    return None


# A pack is JSON content, not a payload to be clever about; 64 MiB is far above any real
# pack and well below anything that would strain the API process.
_UPLOAD_MAX_BYTES = 64 * 1024 * 1024


def _unpack_into(archive: bytes, staging: pathlib.Path) -> pathlib.Path:
    """Extract an uploaded .zip/.tar.gz into ``staging`` and return the pack root.

    Every member is checked to land inside ``staging``: an archive is operator-supplied
    but arrives over the network, and ``..`` members or absolute paths would otherwise
    write anywhere the process can reach. Symlinks are dropped for the same reason --
    a pack is plain JSON and has no use for them.
    """
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    def _safe(name: str) -> pathlib.Path | None:
        target = (staging / name).resolve()
        if target == staging.resolve() or staging.resolve() not in target.parents:
            return None
        return target

    if archive[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                target = _safe(info.filename)
                if target is None:
                    raise PluginSyncError(f"archive entry escapes the pack: {info.filename!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))
    else:
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue  # directories are implied; symlinks/devices are dropped
                    target = _safe(member.name)
                    if target is None:
                        raise PluginSyncError(f"archive entry escapes the pack: {member.name!r}")
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(extracted.read())
        except tarfile.TarError as exc:
            raise PluginSyncError(f"not a readable .zip or .tar.gz archive: {exc}") from exc

    if (staging / "plugin.json").is_file():
        return staging
    # Archives made from a repo (GitHub's "Download ZIP", `tar czf` of a checkout) carry a
    # single wrapping directory. Unwrap it rather than making the user repack.
    children = [c for c in staging.iterdir() if c.is_dir()]
    if len(children) == 1 and (children[0] / "plugin.json").is_file():
        return children[0]
    raise PluginSyncError(
        "plugin.json not found at the archive root (or in a single top-level directory)"
    )


async def install_uploaded_plugin(name: str, archive: bytes) -> PluginRepositoryRow:
    """Install a workflow pack from an uploaded archive -- the manual counterpart to a
    git sync, for deployments that cannot reach the pinned plugin repositories.

    The content is validated exactly as a cloned repository is, and only swapped in once
    it validates, so a bad upload leaves the previous version serving. Registered with
    source='upload' so it survives restarts and can be removed again from the console.
    """
    if not _NAME_RE.match(name):
        raise PluginSyncError("name must be 2-63 chars of lowercase letters, digits, '-' or '_'")
    if name in _SYSTEM_NAMES:
        raise PluginSyncError(f"{name!r} is reserved for the system plugin repositories")
    if not archive:
        raise PluginSyncError("empty upload")
    if len(archive) > _UPLOAD_MAX_BYTES:
        raise PluginSyncError(f"archive exceeds {_UPLOAD_MAX_BYTES // (1024 * 1024)} MiB")

    dest = plugins_root() / name
    staging = plugins_root() / f".{name}.incoming"

    def _land() -> None:
        content = _unpack_into(archive, staging)
        validate_plugin(content)  # fail before the live directory is touched
        if dest.exists():
            shutil.rmtree(dest)
        content.rename(dest)

    try:
        await asyncio.to_thread(_land)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    async with admin_registry_session() as session:
        row = await session.scalar(
            select(PluginRepositoryRow).where(PluginRepositoryRow.name == name)
        )
        if row is None:
            row = PluginRepositoryRow(name=name, url=f"upload:{name}", ref="", source="upload")
            session.add(row)
        elif row.source not in ("upload", "git"):
            raise PluginSyncError(
                f"{name!r} is already provided by a {row.source} plugin repository"
            )
        row.url, row.source = f"upload:{name}", "upload"
        row.ref = content_fingerprint(dest)
        # Flush before reading the id: a freshly added row gets its uuid from the column
        # server_default, so row.id is None until the INSERT actually goes out.
        await session.flush()
        repo_id = row.id
    return await sync_repository(repo_id)


async def _sync_dropped_plugins() -> None:
    """Register every pack sitting in the plugin drop directory.

    Per-plugin failures are recorded on the row and logged, never raised: one malformed
    hand-placed pack must not stop the rest -- or the boot that calls this.
    """
    import structlog

    root = plugin_drop_dir()
    try:
        found = sorted(c for c in root.iterdir() if (c / "plugin.json").is_file())
    except OSError:
        return  # no drop directory mounted -- the ordinary case
    for path in found:
        if path.name in _SYSTEM_NAMES or not _NAME_RE.match(path.name):
            structlog.get_logger().warning("plugins.drop_skipped", directory=path.name)
            continue
        try:
            await _ensure_system_row(
                path.name, "local", f"file://{path}", content_fingerprint(path)
            )
        except PluginSyncError as exc:
            structlog.get_logger().warning(
                "plugins.drop_sync_failed", directory=path.name, error=str(exc)[:300]
            )


async def _ensure_system_row(name: str, source: str, url: str, ref: str) -> None:
    async with admin_registry_session() as session:
        row = await session.scalar(
            select(PluginRepositoryRow).where(PluginRepositoryRow.name == name)
        )
        if row is None:
            row = PluginRepositoryRow(name=name, url=url, ref=ref, source=source)
            session.add(row)
            await session.flush()
        needs_sync = row.status != "synced" or row.ref != ref
        row.url, row.ref, row.source = url, ref, source
        repo_id = row.id
        await session.flush()
        session.expunge(row)
    if needs_sync:
        await sync_repository(repo_id)


async def ensure_default_synced() -> None:
    """Boot-time: register + sync the system plugins so a fresh deployment has working
    workflows without operator action -- the in-tree built-in ('default' workflow) and
    the pinned default plugin repository (baked into the image). Idempotent; each
    re-syncs when its content ref changes (image upgrade)."""
    builtin = builtin_plugin_dir()
    if builtin is not None:
        await _ensure_system_row("builtin", "builtin", "builtin", content_fingerprint(builtin))

    spec_path = pathlib.Path(__file__).resolve().parents[3] / "deploy" / "plugins.json"
    url, ref = "baked", "baked"
    if spec_path.is_file():
        entry = json.loads(spec_path.read_text()).get("default") or {}
        url, ref = str(entry.get("url", url)), str(entry.get("ref", ref))
    await _ensure_system_row("default", "baked", url, ref)

    # Hand-placed packs: the offline/private-repo path, picked up without operator action.
    await _sync_dropped_plugins()
