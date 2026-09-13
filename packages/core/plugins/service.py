"""Plugin-repository sync: registered git repositories become system workflows.

Sync is an operator action running with the admin engine (``admin_registry_session``)
because it writes NULL-tenant rows (``workflow`` templates, global
``vocabulary_overlay``) that the app role's RLS WITH CHECK structurally forbids --
the same reason the admin console owns tenant provisioning. Content is validated
declaratively (pydantic manifests; the pack loader only ever reads JSON) and nothing
from a plugin executes, honoring rule 9/D7.

The **default** plugin's pinned content ships inside the image (``/app/packs``,
copied from ``.plugins/default`` at build -- see deploy/plugins.json), so first boot
syncs without the network. Added repositories are shallow-cloned at their pinned ref
into ``<blob_root>/plugins/<name>``.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import subprocess
import uuid
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
    return plugins_root() / row.name


def _clone(row: PluginRepositoryRow) -> pathlib.Path:
    dest = plugins_root() / row.name
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", "--quiet", row.url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        subprocess.run(
            ["git", "checkout", "--quiet", row.ref],
            cwd=dest,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
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
        content = (
            _content_dir(row)
            if row.source in ("baked", "builtin")
            else await asyncio.to_thread(_clone, row)
        )
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
