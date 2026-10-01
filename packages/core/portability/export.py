"""`.pyr` export.

**The one rule this module exists to keep** : export runs through the *same*
visibility resolution as context assembly. The first thing ``export_workspace`` does is
call ``core.assembler.visibility.scopes_for(..., EXPORT, None)``; every selection after
that is a membership test against the set it returned, or a query with that set pushed
down as a SQL predicate. There is no role lookup here, no membership join, no literal
scope key, and no second notion of "who may see what" -- because two implementations
would drift, and the one that drifted would leak.

That is not a claim to take on faith: ``test_export_service_delegates_all_visibility_to_
the_resolver`` reads this file's source and fails if a visibility term appears anywhere
except the resolver call. INV-1's lint independently keeps ``core.knowledge.repo`` and
``core.secrets.repo`` out, so the read paths available here are the freely-importable
sibling modules (``knowledge.authoring``, ``secrets.authoring``) -- the same pattern
 of ``docs/phase-workflow.md`` documents.

**Omissions are visible.** Every object the filter excluded is recorded in
``manifest.redactions[]`` as ``{type, id, reason}``. A recipient can always distinguish
"this workspace had none" from "you weren't shown them", which is the difference between
a sanitised bundle being honest and being merely quiet.

**Export is a worker job producing a blob, not a synchronous download.** A workspace with
a year of sessions is not something an HTTP request should hold open;
``worker.export.handle_export_workspace`` is the caller, and it stores the result through
``BlobStore``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import func, select
from sqlalchemy import text as sa_text

from core.agents.models import Persona
from core.assembler.models import ContextManifestRow
from core.assembler.visibility import EXPORT, portable_scope_bands, scopes_for
from core.audit.service import AuditService
from core.behavior.repo import list_behavior_profile_versions
from core.entities.repo import list_latest_schemas
from core.entities.storage import list_entities_for_scope
from core.knowledge.authoring import (
    list_source_attachments,
    list_version_entries,
    list_versions,
    list_workspace_attachments,
)
from core.knowledge.models import KnowledgeSource
from core.portability.bundle import BundleWriter
from core.ports.encryptor import Encryptor
from core.ports.permission import PermissionService
from core.process.authoring import list_definitions
from core.resolution.records import ResolutionRecordRow
from core.secrets.authoring import list_holders
from core.secrets.models import SecretRow
from core.sessions.models import CheckpointRow, SessionEventRow, SessionRow
from core.tenancy.models import Principal, Workspace
from core.tenancy.scope import tenant_scope
from core.version import APP_VERSION
from core.vocabulary.service import resolve_overlay_for_workspace

ExportMode = Literal["participant", "full", "sanitised"]
"""The three modes of, and the *only* three. Deliberately a closed Literal rather
than a string: a fourth mode invented at a call site is how a "just this once" export
path gets written that nobody reviews against the leak test.

| mode         | requires          | secret content included                    |
|--------------|-------------------|--------------------------------------------|
| participant  | any workspace role| only secrets this principal **holds**      |
| full         | ``secret:inspect``| all of them, and an audit row says so      |
| sanitised    | any workspace role| none, ever -- redaction stubs instead      |

Note participant is *holder*-scoped, not author-scoped: "my session log" means what I was
told, and an author who happens not to hold their own secret still authored it. The two
questions are different and the export modes are where they stop being conflated."""

_INSPECT_ACTION = "secret:inspect"
_FULL_EXPORT_AUDIT_ACTION = "export:full"


class ExportPermissionDeniedError(Exception):
    """A full export was requested by a principal without ``secret:inspect``. Raised
    *before* any object selection begins -- refusing after assembling the bundle would
    mean the plaintext had already been gathered into memory once."""


# The selectable bundle sections. "personas" covers agents/ + behavior profiles;
# "connections" additionally embeds model connections WITH their decrypted provider
# credentials -- the one genuinely sensitive section, which is why selecting it (or
# mode="full", whose secret plaintext is just as sensitive) forces password encryption.
EXPORT_SECTIONS = frozenset(
    {
        "knowledge",
        "schemas",
        "entities",
        "personas",
        "process",
        "secrets",
        "vocabulary",
        "sessions",
        "connections",
        # Rule systems + the tool definitions that bind them. Mechanics travel with the
        # game: without this a bundle's rolls resolve against whatever the importing tenant
        # happened to have.
        "rules",
        # The organization's verified runtime images, as exact digest-pinned references
        # with their Dockerfile as provenance. Never a builder, a credential or history.
        "images",
    }
)
DEFAULT_SECTIONS = EXPORT_SECTIONS - {"connections"}


class ExportRequiresEncryptionError(Exception):
    """The selected content is sensitive (provider credentials, or full-mode secret
    plaintext) and no password was supplied. Plaintext is not an option for these --
    refusing beats a .pyr full of API keys sitting in someone's Downloads folder."""


@dataclass(frozen=True)
class ExportOptions:
    mode: ExportMode = "participant"
    include_sessions: bool = True
    sections: frozenset[str] = DEFAULT_SECTIONS
    # write-only: never echoed, never stored; used once to seal the bundle
    password: str | None = None


@dataclass(frozen=True)
class ExportResult:
    data: bytes
    manifest: dict[str, Any]

    @property
    def redactions(self) -> list[dict[str, str]]:
        return [dict(r) for r in self.manifest.get("redactions", [])]


async def export_workspace(
    viewer: Principal,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    options: ExportOptions | None = None,
    encryptor: Encryptor,
    permission_service: PermissionService,
    audit_service: AuditService | None = None,
) -> ExportResult:
    """Builds the bundle. ``viewer`` first, mirroring ``ContextAssembler.assemble``'s own
    signature discipline -- who is asking is not an option, it is the question."""
    opts = options or ExportOptions()
    unknown = set(opts.sections) - EXPORT_SECTIONS
    if unknown:
        raise ValueError(f"unknown export sections: {sorted(unknown)}")

    # Sensitivity gate BEFORE any selection: nothing sensitive may leave in the clear.
    #
    # Two different things used to be one rule. Provider credentials are sensitive
    # unconditionally -- they spend money and impersonate people, and there is no opt-out.
    # Full-mode secret plaintext is sensitive *because of what the secrets are*, which is
    # a question only their author can answer, and now does: a workspace whose secrets are
    # all `publishable` is a case written to be handed out, and forcing a password on it
    # would mean the format cannot express the artefact it exists to carry.
    if "connections" in opts.sections and not opts.password:
        raise ExportRequiresEncryptionError(
            "this export includes provider credentials; a password is required -- "
            "plaintext export is unavailable for them"
        )
    if opts.mode == "full" and not opts.password:
        guarded = await count_guarded_secrets(tenant_id, workspace_id)
        if guarded:
            raise ExportRequiresEncryptionError(
                f"this workspace has {guarded} guarded secret(s) and a full export "
                "carries their plaintext; supply a password, or mark them publishable "
                "if they are content written to be shared"
            )

    if opts.mode == "full":
        # Checked here, before anything is selected: a refusal that arrives after the
        # bundle is assembled has already put every secret's plaintext in this process's
        # memory, which is most of what the refusal was for.
        if not await permission_service.check(
            tenant_id, viewer.id, _INSPECT_ACTION, "workspace", workspace_id
        ):
            raise ExportPermissionDeniedError(
                f"principal {viewer.id} may not run a full export of workspace "
                f"{workspace_id}: it requires {_INSPECT_ACTION!r}"
            )
        # As loud as inspecting a single secret, on the same hash chain.
        await (audit_service or AuditService()).append(
            tenant_id=tenant_id,
            actor_principal_id=viewer.id,
            action=_FULL_EXPORT_AUDIT_ACTION,
            resource_type="workspace",
            resource_id=workspace_id,
            query={"mode": "full"},
        )

    # ── The one visibility decision in this module. Everything below filters against
    #    `visible`; nothing below re-derives it.
    visible = frozenset(await scopes_for(tenant_id, viewer.id, workspace_id, EXPORT, None))

    async with tenant_scope(tenant_id) as session:
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None:
            raise ValueError(f"no workspace {workspace_id} in this tenant")
        workspace_json = {
            "id": str(workspace.id),
            "key": workspace.key,
            "name": workspace.name,
            "description": workspace.description,
            "settings": dict(workspace.settings),
            "clock_value": workspace.clock_value,
        }

    # Record the workflow this content was authored under: behaviour profiles reference
    # pack axes, and those only exist for a tenant once that pack is loaded.
    from core.workflows.service import get_tenant_workflow_key

    writer = BundleWriter(
        tenant_ref=str(tenant_id),
        app_version=APP_VERSION,
        workflow_key=await get_tenant_workflow_key(tenant_id) or "",
    )
    writer.add_json("workspace.json", workspace_json)

    if "knowledge" in opts.sections:
        await _add_knowledge(writer, tenant_id, workspace_id, visible)
    if "schemas" in opts.sections:
        await _add_schemas(writer, tenant_id, workspace_id)
    if "entities" in opts.sections:
        await _add_entities(writer, tenant_id, workspace_id, visible)
    if "personas" in opts.sections:
        await _add_agents(writer, tenant_id, workspace_id)
        await _add_scopes(writer, tenant_id, workspace_id)
    if "connections" in opts.sections:
        await _add_connections(writer, tenant_id, workspace_id, encryptor=encryptor)
    if "process" in opts.sections:
        await _add_process(writer, tenant_id, workspace_id)
    # After process: the tools a bundle carries are the ones its flows name, so the
    # definitions have to be readable before this can know what to look for.
    if "rules" in opts.sections:
        await _add_rules(writer, tenant_id, workspace_id)
    if "secrets" in opts.sections:
        await _add_secrets(
            writer,
            tenant_id,
            workspace_id,
            viewer.id,
            visible,
            mode=opts.mode,
            encryptor=encryptor,
        )
    if "vocabulary" in opts.sections:
        await _add_vocabulary(writer, tenant_id, workspace_id)
    if opts.include_sessions and "sessions" in opts.sections:
        await _add_sessions(writer, tenant_id, workspace_id)
    if "images" in opts.sections:
        await _add_images(writer, tenant_id)

    manifest = writer.build_manifest()
    data = writer.seal()
    if opts.password:
        from core.portability.crypto import encrypt_bundle

        data = encrypt_bundle(data, opts.password)
    return ExportResult(data=data, manifest=manifest)


async def _add_images(writer: BundleWriter, tenant_id: uuid.UUID) -> None:
    """Each image whose current build is verified, as the exact reference that was
    checked. A tag never travels: the importer would be trusting whatever it points at
    on the day they import.

    Organization-wide rather than per workspace, like the runtimes they become -- a repo
    picks a runtime, not a workspace. The harness travels as a *claim* only; the importer's
    own smoke test decides whether it is believed.
    """
    from core.images.service import list_images

    for image in await list_images(tenant_id):
        current = image.get("current")
        if not current or current.get("status") != "ready" or not current.get("pinned_ref"):
            continue
        baked = current.get("baked_harness") or {}
        writer.add_json(
            f"images/{image['name']}.json",
            {
                "name": image["name"],
                "image": current["pinned_ref"],
                "dockerfile": image.get("dockerfile") or current.get("dockerfile") or "",
                "harness_claim": (
                    {"key": baked["key"], "version": baked.get("version", "")}
                    if baked.get("key")
                    else None
                ),
            },
        )


# ── knowledge ───────────────────────────────────────────────────────────────────────


async def _add_knowledge(
    writer: BundleWriter, tenant_id: uuid.UUID, workspace_id: uuid.UUID, visible: frozenset[str]
) -> None:
    """Full version history per attached source, with entry bodies as `.md`
    files so a bundle is git-diffable. An entry whose ``scope_key`` is outside the
    resolved set is not written and is redacted by id -- the *file* is absent, not blanked,
    because a blanked file is still a file whose name and size say something."""
    attachments = await list_workspace_attachments(tenant_id, workspace_id)
    for attachment in attachments:
        source_id = attachment.knowledge_source_id
        async with tenant_scope(tenant_id) as session:
            source = await session.get(KnowledgeSource, source_id)
            if source is None:
                continue
            source_json = {
                "id": str(source.id),
                "key": source.key,
                "name": source.name,
                "class": source.class_,
                "visibility": source.visibility,
            }
        prefix = f"knowledge/ks_{source_id}"
        writer.add_json(f"{prefix}/meta.json", source_json)

        source_attachments = await list_source_attachments(tenant_id, source_id)
        writer.add_json(
            f"{prefix}/attachments.json",
            [
                {
                    "workspace_id": str(a.workspace_id),
                    "scope_key": a.scope_key,
                    "priority_weight": str(a.priority_weight),
                    "version_pin": str(a.version_pin) if a.version_pin else None,
                    "enabled": a.enabled,
                }
                for a in source_attachments
                if a.workspace_id == workspace_id
            ],
        )

        for version in await list_versions(tenant_id, source_id):
            entries = await list_version_entries(tenant_id, version.id)
            included = [e for e in entries if e.scope_key in visible]
            for excluded in (e for e in entries if e.scope_key not in visible):
                writer.redact(
                    "knowledge_entry",
                    str(excluded.id),
                    f"scope {excluded.scope_key!r} not visible to the exporting principal",
                )
            writer.add_json(
                f"{prefix}/versions/v{version.version_number}.json",
                {
                    "id": str(version.id),
                    "version_number": version.version_number,
                    "content_hash": version.content_hash,
                    "parent_version_id": (
                        str(version.parent_version_id) if version.parent_version_id else None
                    ),
                    "entry_ids": [str(e.id) for e in included],
                },
            )
            for entry in included:
                writer.add_json(
                    f"{prefix}/entries/v{version.version_number}/{entry.entry_key}.meta.json",
                    _entry_meta(entry),
                )
                writer.add_text(
                    f"{prefix}/entries/v{version.version_number}/{entry.entry_key}.md",
                    entry.body_md,
                )

        await _add_chunks(writer, tenant_id, source_id, visible, prefix)


async def _add_chunks(
    writer: BundleWriter,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    visible: frozenset[str],
    prefix: str,
) -> None:
    """Chunk texts, keyed by chunk id -- not part of the original bundle layout, and added
    deliberately.

    A ``ContextManifest`` records which *chunks* a turn included, and a turn's rendered
    context is built from chunk bodies, not entry bodies (an entry may split into several
    chunks). Without them, "replay a turn from the bundle alone" is not merely untested,
    it is arithmetically impossible -- the bundle would be missing the strings the hash was
    taken over. Same scope filter as entries: a chunk outside the resolved set is absent
    and redacted, so a sanitised bundle cannot smuggle a body back in through the chunk
    table.
    """
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                sa_text(
                    "SELECT c.id, c.entry_id, c.ordinal, c.text, c.token_count, c.class, "
                    "c.scope_key, c.version_id "
                    "FROM knowledge_chunk c JOIN knowledge_entry e ON e.id = c.entry_id "
                    "WHERE c.tenant_id = :tenant_id AND e.knowledge_source_id = :source_id "
                    "ORDER BY c.entry_id, c.ordinal"
                ),
                {"tenant_id": tenant_id, "source_id": source_id},
            )
        ).all()

    for row in rows:
        chunk_id, entry_id, ordinal, chunk_text, token_count, class_, scope_key, version_id = row
        if scope_key not in visible:
            writer.redact(
                "knowledge_chunk",
                str(chunk_id),
                f"scope {scope_key!r} not visible to the exporting principal",
            )
            continue
        writer.add_json(
            f"{prefix}/chunks/{chunk_id}.json",
            {
                "id": str(chunk_id),
                "entry_id": str(entry_id),
                "version_id": str(version_id) if version_id else None,
                "ordinal": ordinal,
                "token_count": token_count,
                "class": class_,
                "scope_key": scope_key,
                "text": chunk_text,
            },
        )


def _entry_meta(entry: Any) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "entry_key": entry.entry_key,
        "title": entry.title,
        "class": entry.class_,
        "scope_key": entry.scope_key,
        "keys": list(entry.keys or []),
        "secondary_keys": list(entry.secondary_keys or []),
        "logic": entry.logic,
        "use_regex": entry.use_regex,
        "constant": entry.constant,
        "sticky": entry.sticky,
        "cooldown": entry.cooldown,
        "delay": entry.delay,
        "trigger_pct": entry.trigger_pct,
        "inclusion_group": entry.inclusion_group,
        "position": entry.position,
        "insertion_order": entry.insertion_order,
    }


# ── schemas, entities, agents, process ──────────────────────────────────────────────


async def _add_schemas(writer: BundleWriter, tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    """Schemas carry no scope of their own -- they are shape, not content, and a schema
    tells you a field exists, never what any instance holds. Entity *instances* are where
    the filtering bites, below."""
    for schema in await list_latest_schemas(tenant_id, workspace_id):
        writer.add_json(
            f"schemas/entity_schema_{schema.id}.json",
            {
                "id": str(schema.id),
                "key": schema.key,
                "version": schema.version,
                "definition": schema.to_definition().model_dump(mode="json"),
                "ai_assisted": schema.ai_assisted,
            },
        )


async def _add_entities(
    writer: BundleWriter, tenant_id: uuid.UUID, workspace_id: uuid.UUID, visible: frozenset[str]
) -> None:
    """Pushed down as a SQL predicate, not filtered afterwards (INV-4's discipline applied
    to the export path): ``list_entities_for_scope`` requires the set and refuses an empty
    one, so there is no code path here that could select an entity and then forget to drop
    it."""
    included = await list_entities_for_scope(tenant_id, workspace_id, visible) if visible else []
    included_ids = {e.id for e in included}
    for entity in included:
        writer.add_json(
            f"entities/entity_{entity.id}.json",
            {
                "id": str(entity.id),
                "schema_id": str(entity.schema_id),
                "key": entity.key,
                "name": entity.name,
                "scope_key": entity.scope_key,
                "version": entity.version,
                "data": entity.data,
                # An entity mid-session is bloodied, or its work item is in review.
                # Carrying only ``data`` exported the shape and dropped the situation.
                "fsm_states": entity.fsm_states,
            },
        )

    async with tenant_scope(tenant_id) as session:
        from core.entities.storage import EntityRow

        all_ids = (
            await session.execute(
                select(EntityRow.id, EntityRow.scope_key).where(
                    EntityRow.workspace_id == workspace_id
                )
            )
        ).all()
    for entity_id, scope_key in all_ids:
        if entity_id not in included_ids:
            writer.redact(
                "entity",
                str(entity_id),
                f"scope {scope_key!r} not visible to the exporting principal",
            )


async def _add_agents(writer: BundleWriter, tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    """Agents and their behaviour-profile versions. ``agent_id`` travels as
    a reference only -- a bundle never carries a provider credential, and
    ``agent.credential_ref`` points into a secret manager rather than holding a
    key, so there is nothing here for an export to accidentally leak."""
    async with tenant_scope(tenant_id) as session:
        agents = list(
            (
                await session.execute(select(Persona).where(Persona.workspace_id == workspace_id))
            ).scalars()
        )
        for agent in agents:
            session.expunge(agent)

    for agent in agents:
        versions = await list_behavior_profile_versions(tenant_id, agent.id)
        writer.add_json(
            f"agents/agent_{agent.id}.json",
            {
                "id": str(agent.id),
                "key": agent.key,
                "name": agent.name,
                "persona_type": agent.persona_type,
                "persona_md": agent.persona_md,
                "params": dict(agent.params or {}),
                # Whether this persona may reach the internet. A bundle that leaves it
                # out imports a cast that cannot search, which for a sample built around
                # research is the whole sample missing -- and silently, since a persona
                # with no search tool simply writes from memory.
                "web_search": bool(agent.web_search),
                # Which registered coding harness this persona's delegated work runs
                # through. Same reasoning as web_search, and the same failure if it is
                # left out: a sample built around an agent that reads, edits and runs the
                # tests imports a developer that can do none of those, and says nothing
                # about it. The *key* travels, never a command -- what it names is
                # resolved against the importing deployment's own registry.
                "harness": agent.harness,
                "model_profile_ref": str(agent.agent_id),
                "behavior_profile_versions": [
                    {
                        "version": v.version,
                        "pack_id": v.pack_id,
                        "axis_values": dict(v.axis_values),
                    }
                    for v in versions
                ],
            },
        )


async def _add_scopes(writer: BundleWriter, tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    """Group scope rows -- the "levels of lore" bands.

    Knowledge already travels with its ``scope_key``, but without the band itself a
    restricted source lands in the target workspace pointing at a scope that does not
    exist, and fails closed: unreachable by everyone, including the personas it was
    written for. That is safe and useless.

    Members travel as **persona keys**, never principal ids: a principal id is
    meaningless in the importing tenant, while a persona key is exactly what the importer
    has just recreated. The seeded default scopes are re-created by
    the importer itself, so only non-default bands are carried; private compartments are
    a convention rather than rows and are never exported.
    """
    bands = await portable_scope_bands(tenant_id, workspace_id)
    if not bands:
        return

    async with tenant_scope(tenant_id) as session:
        personas = list(
            (
                await session.execute(select(Persona).where(Persona.workspace_id == workspace_id))
            ).scalars()
        )
        key_by_principal = {str(p.principal_id): p.key for p in personas}

    for band in bands:
        # A member with no persona in this workspace (the human builder, an operator) has
        # no portable identity; drop it rather than invent one. The importer re-admits
        # whoever performs the import, so an imported band is never orphaned.
        persona_keys = [
            key_by_principal[pid]
            for pid in cast("list[Any]", band["principal_ids"])
            if pid in key_by_principal
        ]
        writer.add_json(
            f"scopes/scope_{band['key']}.json",
            {
                "key": band["key"],
                "kind": band["kind"],
                "persona_keys": persona_keys,
                "roles": band["roles"],
            },
        )


async def _add_connections(
    writer: BundleWriter, tenant_id: uuid.UUID, workspace_id: uuid.UUID, *, encryptor: Encryptor
) -> None:
    """Model connections used by this workspace's personas, WITH their decrypted
    provider credentials -- opt-in only, and only into a password-encrypted bundle
    (export_workspace enforces that before this runs). This is the one section a
    bundle carries that could spend money or impersonate anyone."""
    from core.agents.authoring import resolve_connection_api_key

    async with tenant_scope(tenant_id) as session:
        personas = (
            (await session.execute(select(Persona).where(Persona.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        connection_ids = {p.agent_id for p in personas}
        from core.agents.models import Agent as ConnectionRow

        connections = (
            (
                await session.execute(
                    select(ConnectionRow).where(ConnectionRow.id.in_(connection_ids))
                )
            )
            .scalars()
            .all()
        )
        for connection in connections:
            session.expunge(connection)

    records = []
    for connection in connections:
        api_key = await resolve_connection_api_key(
            tenant_id, connection.credential_ref, encryptor=encryptor
        )
        records.append(
            {
                "id": str(connection.id),
                "name": connection.name,
                "provider": connection.provider,
                "model": connection.model,
                "api_base": connection.api_base,
                "params": dict(connection.params or {}),
                "api_key": api_key,  # plaintext INSIDE the encrypted envelope only
            }
        )
    writer.add_json("connections/connections.json", records)


async def _add_process(writer: BundleWriter, tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    for definition in await list_definitions(tenant_id, workspace_id=workspace_id):
        writer.add_json(
            f"process/process_def_{definition.id}.json",
            {
                "id": str(definition.id),
                "key": definition.key,
                "version": definition.version,
                "name": definition.name,
                "definition": definition.definition,
            },
        )


async def _add_rules(writer: BundleWriter, tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    """The mechanics a bundle needs to resolve its own rolls: the tool definitions this
    workspace's flows name, and the rule systems those tools validate against.

    Without this a sample could carry a setting, a cast and a flow, and then resolve every
    roll against whatever generic system happened to exist in the importing tenant -- so
    "run this one-shot" meant "install a workflow pack first, and hope it is the right
    one". Mechanics are the half of a game that has to travel with it.

    Scoped to what the workspace's process definitions actually reference, not to every
    row in the tenant: rule systems and tool definitions are tenant-wide (no workspace_id
    on either table), so exporting them wholesale would put another workspace's rulesets
    in this bundle. A tool names its rule system through ``validation_ref``; that is the
    edge this walks.

    Neither carries anything sensitive -- a tool definition is declarative metadata whose
    ``impl_ref`` is descriptive and never evaluated (see core.resolution.registry), and a
    rule system is arithmetic. There is no credential or scope-bearing content here, which
    is why this rides in the default sections rather than behind the encryption gate.
    """
    from core.resolution.registry import list_tool_definitions
    from core.resolution.rule_system import get_rule_system

    wanted: set[str] = set()
    for definition in await list_definitions(tenant_id, workspace_id=workspace_id):
        phases = definition.definition.get("phases")
        if not isinstance(phases, dict):
            continue
        for phase in phases.values():
            if not isinstance(phase, dict):
                continue
            for key in phase.get("tools") or []:
                wanted.add(str(key))
            for key in phase.get("remote_tools") or []:
                wanted.add(str(key))
    if not wanted:
        return

    rule_system_keys: set[str] = set()
    for tool in await list_tool_definitions(tenant_id):
        if tool.key not in wanted:
            continue
        writer.add_json(
            f"rules/tool_{tool.key}.json",
            {
                "key": tool.key,
                "kind": tool.kind,
                "input_schema": tool.input_schema,
                "output_schema": tool.output_schema,
                "impl_ref": tool.impl_ref,
                "validation_ref": tool.validation_ref,
                "determinism": tool.determinism,
            },
        )
        if tool.validation_ref:
            rule_system_keys.add(str(tool.validation_ref))

    for key in sorted(rule_system_keys):
        row = await get_rule_system(tenant_id, key)
        if row is None:
            continue
        writer.add_json(
            f"rules/rule_system_{row.key}.json",
            {
                "key": row.key,
                "name": row.name,
                "expression_grammar": row.expression_grammar,
                "check_types": row.check_types,
                "outcome_bands": row.outcome_bands,
                "modifier_resolver": row.modifier_resolver,
                "validators": row.validators,
            },
        )


# ── secrets ─────────────────────────────────────────────────────────────────────────


async def _add_secrets(
    writer: BundleWriter,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    viewer_id: uuid.UUID,
    visible: frozenset[str],
    *,
    mode: ExportMode,
    encryptor: Encryptor,
) -> None:
    """The one place the three modes actually differ.

    **Sanitised** writes no secret content at all -- not blanked, not empty-stringed:
    ``_secret_metadata`` is the only shape it can produce, so there is no branch in which a
    plaintext field could be assigned. Every secret is redacted by id, so a recipient sees
    the shape of what was withheld.

    **Participant** includes a secret's content only when this principal is a *holder* of
    it. Holder, not author: "my session log" means what I was told. the ``SecretView``
    keys its plaintext on authorship, which answers a different question, so this reads
    ``secret_holder`` directly through ``list_holders`` (a freely-importable sibling, not
    ``secrets.repo`` -- INV-1) and re-decrypts only for the ones that pass.

    **Full** includes everything, and got its ``secret:inspect`` check and audit row
    before this function was ever called.

    Scope filtering applies in every mode, first: a secret outside the exporter's resolved
    scope set is never a candidate, whatever mode asked."""
    async with tenant_scope(tenant_id) as session:
        rows = list(
            (
                await session.execute(
                    select(SecretRow).where(SecretRow.workspace_id == workspace_id)
                )
            ).scalars()
        )
        for row in rows:
            session.expunge(row)
        # Which persona each principal is, so a holder can travel as a persona reference.
        # A principal id means nothing in another tenant; a persona in the same bundle
        # does. Without this a secret arrives held by nobody -- structurally present and
        # behaviourally inert, which is the worst of both.
        persona_by_principal = {
            str(p.principal_id): str(p.id)
            for p in (
                await session.execute(select(Persona).where(Persona.workspace_id == workspace_id))
            ).scalars()
        }

    for row in rows:
        if row.scope_key not in visible:
            writer.redact(
                "secret",
                str(row.id),
                f"scope {row.scope_key!r} not visible to the exporting principal",
            )
            continue

        payload = _secret_metadata(row)
        holder_rows = await list_holders(tenant_id, row.id)
        include_content = False
        if mode == "full":
            include_content = True
        elif mode == "participant":
            include_content = any(h.holder_principal_id == viewer_id for h in holder_rows)

        if include_content:
            payload["content"] = encryptor.decrypt(row.content_ciphertext)
            payload["hint_text"] = row.hint_text
            payload["behavioral_directive"] = row.behavioral_directive
            # Holders travel only alongside the content they explain. A holder set is
            # itself a who-knows-what fact, so a sanitised bundle -- which carries no
            # content at all -- must not disclose one either; and an import has nothing
            # to attach holders to without the content.
            payload["holders"] = [
                {
                    "persona_ref": persona_by_principal[str(h.holder_principal_id)],
                    "holder_kind": h.holder_kind,
                }
                for h in holder_rows
                if str(h.holder_principal_id) in persona_by_principal
            ]
            for holder in holder_rows:
                if str(holder.holder_principal_id) not in persona_by_principal:
                    # A person, or a persona of another workspace: no counterpart exists
                    # in the importing tenant, so say so rather than drop it silently.
                    writer.redact(
                        "secret_holder",
                        str(row.id),
                        f"holder {holder.holder_principal_id} is not a persona of this "
                        "workspace and cannot be resolved in another tenant",
                    )
        else:
            writer.redact(
                "secret_content",
                str(row.id),
                "sanitised export: no secret content in any mode"
                if mode == "sanitised"
                else "the exporting principal does not hold this secret",
            )
        writer.add_json(f"secrets/secret_{row.id}.json", payload)


async def count_guarded_secrets(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> int:
    """How many of this workspace's secrets are NOT declared publishable.

    Deliberately counts the whole workspace rather than only what this exporter can see:
    the question is "could this bundle carry guarded plaintext", and answering it from a
    viewer-narrowed set would make the encryption requirement depend on who is asking.
    Reads no ciphertext -- the refusal must not be the thing that pulls plaintext into
    memory.

    Public because the API route needs the same answer *synchronously*: export itself runs
    in a worker, so a refusal raised here would reach the user as a failed job rather than
    a 422. The route calls this function rather than re-deriving the rule -- one
    implementation, two call sites, which is the whole lesson of."""
    async with tenant_scope(tenant_id) as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(SecretRow)
                .where(
                    SecretRow.workspace_id == workspace_id,
                    SecretRow.publication != "publishable",
                )
            )
            or 0
        )


def _secret_metadata(row: Any) -> dict[str, Any]:
    """Everything about a secret that is *not* one of its four sensitive fields. Factored
    out so the sanitised path has no code path that could reach for content even by
    accident -- there is nothing to omit, because there is nothing to include."""
    return {
        "id": str(row.id),
        "subject_kind": row.subject_kind,
        "subject_id": str(row.subject_id) if row.subject_id else None,
        "gist": row.gist,
        "disclosure_state": row.disclosure_state,
        "scope_key": row.scope_key,
        "publication": row.publication,
        "version": row.version,
    }


# ── vocabulary + sessions ───────────────────────────────────────────────────────────


async def _add_vocabulary(
    writer: BundleWriter, tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> None:
    overlay = await resolve_overlay_for_workspace(tenant_id, workspace_id)
    if overlay is None:
        return
    writer.add_json(
        f"vocabulary/overlay_{overlay.id}.json",
        {"id": str(overlay.id), "key": overlay.key, "name": overlay.name, "labels": overlay.labels},
    )


async def _add_sessions(
    writer: BundleWriter, tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> None:
    """``manifests.jsonl`` is what makes INV-10 work across the export boundary:
    a turn's exact context is reconstructible from the bundle alone six months later.
    ``resolutions.jsonl`` carries seeds, records, and the hash chain, so an archived
    session can prove nobody edited its rolls.

    Session *content* is not scope-filtered here: a session transcript is shared by
    construction (the same boundary the summariser documents -- ``message`` carries no
    scope column, because every principal in the session saw every message live). What a
    given principal may see *about* a session is decided by the knowledge, entity, and
    secret filters above, which is where the per-principal data actually is."""
    async with tenant_scope(tenant_id) as session:
        sessions = list(
            (
                await session.execute(
                    select(SessionRow).where(SessionRow.workspace_id == workspace_id)
                )
            ).scalars()
        )
        for row in sessions:
            session.expunge(row)

    for sess in sessions:
        prefix = f"sessions/session_{sess.id}"
        writer.add_json(
            f"{prefix}/meta.json",
            {
                "id": str(sess.id),
                "persona_id": str(sess.persona_id),
                "current_phase": sess.current_phase,
                "status": sess.status,
                "state": dict(sess.state),
                "process_definition_id": (
                    str(sess.process_definition_id) if sess.process_definition_id else None
                ),
                "process_definition_version": sess.process_definition_version,
            },
        )

        async with tenant_scope(tenant_id) as session:
            events = list(
                (
                    await session.execute(
                        select(SessionEventRow)
                        .where(SessionEventRow.session_id == sess.id)
                        .order_by(SessionEventRow.event_seq)
                    )
                ).scalars()
            )
            checkpoints = list(
                (
                    await session.execute(
                        select(CheckpointRow)
                        .where(CheckpointRow.session_id == sess.id)
                        .order_by(CheckpointRow.event_seq)
                    )
                ).scalars()
            )
            manifests = list(
                (
                    await session.execute(
                        select(ContextManifestRow)
                        .where(ContextManifestRow.session_id == sess.id)
                        .order_by(ContextManifestRow.event_seq)
                    )
                ).scalars()
            )
            resolutions = list(
                (
                    await session.execute(
                        select(ResolutionRecordRow)
                        .where(ResolutionRecordRow.session_id == sess.id)
                        .order_by(ResolutionRecordRow.event_seq)
                    )
                ).scalars()
            )

        writer.add_jsonl(
            f"{prefix}/events.jsonl",
            [
                {
                    "event_seq": e.event_seq,
                    "kind": e.kind,
                    "payload": e.payload,
                    "actor_principal_id": (
                        str(e.actor_principal_id) if e.actor_principal_id else None
                    ),
                }
                for e in events
            ],
        )
        writer.add_jsonl(
            f"{prefix}/checkpoints.jsonl",
            [
                {
                    "event_seq": c.event_seq,
                    "phase": c.phase,
                    "state": c.state,
                    "actor_cursor": c.actor_cursor,
                    "entity_versions": c.entity_versions,
                    "knowledge_version_pins": c.knowledge_version_pins,
                }
                for c in checkpoints
            ],
        )
        writer.add_jsonl(
            f"{prefix}/manifests.jsonl",
            [
                {
                    "event_seq": m.event_seq,
                    "viewer_principal_id": str(m.viewer_principal_id),
                    "phase": m.phase,
                    "entries": m.entries,
                    "redactions": m.redactions,
                    "entity_versions": m.entity_versions,
                    "token_counts": m.token_counts,
                    "rendered_hash": m.rendered_hash,
                    "history_summary_from_seq": m.history_summary_from_seq,
                    "history_summary_to_seq": m.history_summary_to_seq,
                    "history_summary_hash": m.history_summary_hash,
                }
                for m in manifests
            ],
        )
        writer.add_jsonl(
            f"{prefix}/resolutions.jsonl",
            [
                {
                    "event_seq": r.event_seq,
                    "tool_key": r.tool_key,
                    # actor_entity_id / rule_system_id / rule_citation_ids are not
                    # decoration: `compute_row_hash` takes them, so a chain the importer
                    # can actually *verify*  needs every field the hash was over.
                    "actor_entity_id": str(r.actor_entity_id) if r.actor_entity_id else None,
                    "expression": r.expression,
                    "seed": r.seed,
                    "rolls": r.rolls,
                    "modifiers": r.modifiers,
                    "total": r.total,
                    "target": r.target,
                    "outcome": r.outcome,
                    "rule_system_id": str(r.rule_system_id),
                    "rule_citation_ids": [str(c) for c in r.rule_citation_ids],
                    "prev_hash": r.prev_hash,
                    "row_hash": r.row_hash,
                }
                for r in resolutions
            ],
        )
