"""Generic pack loader (its own subtask, reused unmodified by F3.8/F3.13/F3.9): reads
a shipped pack's on-disk JSON content and materializes it into a tenant through the
*exact same* save/authoring functions manual authoring would use -- entity schemas,
rule systems, tool definitions, process definitions, axis
definitions, and library-tenant knowledge seed content.

**Zero pack-name literals, on purpose.** This module never references "rpg"/
"enterprise"/"swdev" -- it walks whichever subdirectories exist under a given
``pack_dir`` (``schemas/``, ``rule_systems/*/``, ``tools/``, ``processes/``, ``axes/``,
``seed/``) with no per-pack special-casing. Adding a pack is adding a directory, not a
branch here -- the whole point the data-driven matrix and core-side literal lint
depend on.

**Packs are content, not code (rule 9).** Every file this module reads is JSON; the
Pydantic/dataclass models it validates against already exist for manually-authored
content (a human editing a schema in a future UI goes through the identical
``EntitySchemaDefinition``/``save_schema`` path). This loader is the only thing that
knows pack directories exist at all -- ``packs/`` itself imports nothing from
``packages/core/`` (the reverse direction is what the packs-independence lint forbids).
"""

from __future__ import annotations

import json
import pathlib
import uuid
from dataclasses import dataclass, field

from core.behavior.repo import create_axis_definition
from core.behavior.validation import AxisDefinitionSchema
from core.entities.repo import next_version, save_schema
from core.entities.schema import EntitySchemaDefinition
from core.knowledge.authoring import EntryFields, list_sources
from core.knowledge.library import LIBRARY_TENANT_ID, seed_library_source
from core.process.authoring import create_definition
from core.resolution.registry import ToolDefinitionSchema, register_tool_definition
from core.resolution.rule_system import RuleSystemDefinitionSchema, create_rule_system


@dataclass(frozen=True)
class LoadedPack:
    pack_key: str
    schema_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    rule_system_keys: list[str] = field(default_factory=list)
    tool_keys: list[str] = field(default_factory=list)
    process_definition_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    axis_keys: list[str] = field(default_factory=list)
    seed_source_keys: list[str] = field(default_factory=list)


def _read_json_files(directory: pathlib.Path) -> list[dict[str, object]]:
    if not directory.is_dir():
        return []
    loaded: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.json")):
        loaded.append(json.loads(path.read_text()))
    return loaded


async def _load_schemas(
    pack_dir: pathlib.Path, tenant_id: uuid.UUID, workspace_id: uuid.UUID | None
) -> dict[str, uuid.UUID]:
    schema_ids: dict[str, uuid.UUID] = {}
    for raw in _read_json_files(pack_dir / "schemas"):
        key = str(raw["key"])
        definition = EntitySchemaDefinition.model_validate(raw["definition"])
        version = await next_version(tenant_id, workspace_id, key)
        row = await save_schema(tenant_id, workspace_id, key, version, definition)
        schema_ids[key] = row.id
    return schema_ids


async def _load_rule_systems(pack_dir: pathlib.Path, tenant_id: uuid.UUID) -> list[str]:
    rule_systems_dir = pack_dir / "rule_systems"
    if not rule_systems_dir.is_dir():
        return []
    keys: list[str] = []
    for name_dir in sorted(rule_systems_dir.iterdir()):
        definition_file = name_dir / "definition.json"
        if not definition_file.is_file():
            continue
        definition = RuleSystemDefinitionSchema.model_validate(
            json.loads(definition_file.read_text())
        )
        await create_rule_system(tenant_id, definition)
        keys.append(definition.key)
    return keys


async def _load_tools(pack_dir: pathlib.Path, tenant_id: uuid.UUID) -> list[str]:
    keys: list[str] = []
    for raw in _read_json_files(pack_dir / "tools"):
        definition = ToolDefinitionSchema.model_validate(raw)
        await register_tool_definition(tenant_id, definition)
        keys.append(definition.key)
    return keys


async def _load_processes(
    pack_dir: pathlib.Path, tenant_id: uuid.UUID, workspace_id: uuid.UUID | None
) -> dict[str, uuid.UUID]:
    ids: dict[str, uuid.UUID] = {}
    for raw in _read_json_files(pack_dir / "processes"):
        key = str(raw["key"])
        name = str(raw.get("name", key))
        definition_raw = raw["definition"]
        assert isinstance(definition_raw, dict)
        row = await create_definition(
            tenant_id, key, name, definition_raw, workspace_id=workspace_id
        )
        ids[key] = row.id
    return ids


async def _load_axes(pack_dir: pathlib.Path, tenant_id: uuid.UUID) -> list[str]:
    keys: list[str] = []
    for raw in _read_json_files(pack_dir / "axes"):
        definition = AxisDefinitionSchema.model_validate(raw)
        await create_axis_definition(tenant_id, definition)
        keys.append(definition.key)
    return keys


def _entry_fields(entry: dict[str, object], default_class: str) -> EntryFields:
    """``EntryFields.class_`` is spelled ``"class"`` in authored JSON (``class`` is a
    Python keyword) -- the one key this loader renames on the way in. Falls back to
    the source's own top-level ``"class"`` when an entry doesn't repeat it (the common
    case: every entry in a source usually shares the source's class)."""
    kwargs = {k: v for k, v in entry.items() if k not in ("entry_key", "class")}
    kwargs["class_"] = entry.get("class", default_class)
    return EntryFields(**kwargs)  # type: ignore[arg-type]


async def _load_seed(pack_dir: pathlib.Path) -> list[str]:
    """Library-tenant seed content -- always the reserved library tenant,
    never the caller's own ``tenant_id`` (seed content is shipped-once, forked-on-edit
    per consuming tenant, not per-tenant provisioned). Idempotent: skips a source key
    that already exists in the library tenant, so re-running a pack load against the
    same persistent database (this repo's own testing convention) never double-seeds
    or errors on a duplicate key."""
    seed_dir = pack_dir / "seed"
    if not seed_dir.is_dir():
        return []
    existing_keys = {source.key for source in await list_sources(LIBRARY_TENANT_ID)}
    loaded_keys: list[str] = []
    for raw in _read_json_files(seed_dir):
        key = str(raw["key"])
        loaded_keys.append(key)
        if key in existing_keys:
            continue
        entries_raw = raw["entries"]
        assert isinstance(entries_raw, list)
        default_class = str(raw["class"])
        entries = [
            (str(entry["entry_key"]), _entry_fields(entry, default_class)) for entry in entries_raw
        ]
        await seed_library_source(key, str(raw["name"]), default_class, entries)
    return loaded_keys


async def load_pack(
    pack_dir: pathlib.Path,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID | None = None,
) -> LoadedPack:
    """Loads every content kind a pack directory declares, in dependency order (schemas
    before processes/tools that might reference them by key, though nothing here
    enforces cross-references beyond what each save path already validates on its
    own). Re-runnable: every underlying save function is either an idempotent upsert
    (rule systems, tools, axes) or a new version (schemas, process definitions,
    matching their own established shape) -- never a hard failure on reload, except
    seed content, made idempotent explicitly above."""
    schema_ids = await _load_schemas(pack_dir, tenant_id, workspace_id)
    rule_system_keys = await _load_rule_systems(pack_dir, tenant_id)
    tool_keys = await _load_tools(pack_dir, tenant_id)
    process_definition_ids = await _load_processes(pack_dir, tenant_id, workspace_id)
    axis_keys = await _load_axes(pack_dir, tenant_id)
    seed_source_keys = await _load_seed(pack_dir)

    return LoadedPack(
        pack_key=pack_dir.name,
        schema_ids=schema_ids,
        rule_system_keys=rule_system_keys,
        tool_keys=tool_keys,
        process_definition_ids=process_definition_ids,
        axis_keys=axis_keys,
        seed_source_keys=seed_source_keys,
    )
