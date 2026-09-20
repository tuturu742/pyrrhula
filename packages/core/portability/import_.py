"""`.pyr` import (G4.6, plan §11.2/§16.6, req 23/24).

Four properties, each an acceptance criterion, and each a decision worth stating:

**The bundle is attacker-controlled input.** Not "might be" -- is. Q2 killed the
marketplace, not the threat; out-of-band sharing means bundles arrive from strangers
anyway, and their knowledge entries are headed for a tool-calling agent's context. Every
imported body runs through ``injection_scan``, and anything flagged imports
**quarantined**: present in the database, absent from retrieval, until a human clears it.
Warn-and-ingest would be theatre -- the entry is in the context either way.

**Verify before trusting, and say *where*.** Integrity hashes are checked file by file and
the resolution hash chain is recomputed record by record, both before a single row is
written. A broken chain reports the exact ``(session, event_seq)`` where the recomputed
hash diverges: "your dice history was edited" is only actionable if it comes with a place
to look.

**Nothing existing is ever overwritten.** Colliding keys fork -- the incoming object takes
a suffixed key and the resident one is untouched. Import is additive by construction, so
there is no import that can destroy work, which means there is no import a user has to be
brave to run.

**History survives.** Sessions, events, checkpoints, manifests, and resolutions import
intact, with ids remapped consistently, so an imported session still replays (INV-10
across the boundary -- ``core.portability.replay`` proves it from bundle contents, and this
module preserves the same relationships in the database).

Imported entity state changes carry ``cause='import'`` with the bundle as ``cause_ref``:
provenance for a row nobody in this tenant ever caused.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.ports.embedding import EmbeddingProvider
    from core.ports.encryptor import Encryptor
    from core.ports.moderation import ModerationProvider
    from core.ports.permission import PermissionService

from sqlalchemy import select, text

from core.agents.models import Persona
from core.audit.hashing import compute_row_hash
from core.entities.fsm import EntityStateChangeRow
from core.entities.repo import get_schema, save_schema
from core.entities.schema import EntitySchemaDefinition
from core.entities.storage import create_entity
from core.knowledge.models import KnowledgeEntry, KnowledgeSource, KnowledgeSourceVersion
from core.portability.bundle import BundleIntegrityError, open_bundle, verify_bundle
from core.portability.injection_scan import quarantine_reason, scan_text
from core.portability.upcast import upcast_to_current
from core.resolution.records import resolution_payload
from core.sessions.models import CheckpointRow, SessionEventRow, SessionRow
from core.tenancy.scope import tenant_scope


class ResolutionChainBrokenError(Exception):
    """The imported dice history does not hash to what it claims. Carries the exact
    location so the message is actionable rather than merely alarming."""

    def __init__(self, session_ref: str, event_seq: int, detail: str) -> None:
        self.session_ref = session_ref
        self.event_seq = event_seq
        self.detail = detail
        super().__init__(
            f"resolution hash chain breaks in session {session_ref} at event_seq "
            f"{event_seq}: {detail}"
        )


@dataclass
class ImportReport:
    """What happened, in the shape the review UI needs: what came in, what forked, what
    was quarantined and why."""

    knowledge_sources: int = 0
    entries: int = 0
    quarantined_entries: list[tuple[str, str]] = field(default_factory=list)
    forked_keys: list[tuple[str, str]] = field(default_factory=list)
    sessions: int = 0
    events: int = 0
    resolutions: int = 0
    id_map: dict[str, str] = field(default_factory=dict)
    # persona/connection import (the half the importer used to drop)
    imported: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # bundle persona ref -> the principal id its imported persona now acts as. Secrets
    # need it: a holder travels as a persona, but is held by that persona's principal.
    persona_principals: dict[str, uuid.UUID] = field(default_factory=dict)

    @property
    def quarantined_count(self) -> int:
        return len(self.quarantined_entries)


def verify_resolution_chain(
    records: list[dict[str, Any]], tenant_ref: str, session_ref: str
) -> None:
    """Recomputes every record's ``row_hash`` from its own payload and its predecessor's
    hash, exactly the way ``core.resolution.service`` computed it. Raises at the *first*
    divergence, naming it -- later records inherit a broken prefix, so reporting all of
    them would be reporting one fault many times.

    ``tenant_ref`` is the *exporting* tenant, read from the manifest: the hash was taken
    over that id, and recomputing it against the importing tenant's id would fail every
    honest bundle."""
    previous: str | None = None
    for record in records:
        payload = resolution_payload(
            tenant_id=uuid.UUID(tenant_ref),
            session_id=uuid.UUID(session_ref),
            event_seq=int(record["event_seq"]),
            tool_key=str(record["tool_key"]),
            actor_entity_id=(
                uuid.UUID(record["actor_entity_id"]) if record.get("actor_entity_id") else None
            ),
            expression=str(record["expression"]),
            seed=str(record["seed"]),
            rolls=list(record["rolls"]),
            modifiers=dict(record["modifiers"]),
            total=int(record["total"]),
            target=record.get("target"),
            outcome=str(record["outcome"]),
            rule_system_id=uuid.UUID(record["rule_system_id"]),
            rule_citation_ids=[uuid.UUID(c) for c in record.get("rule_citation_ids", [])],
        )
        expected = compute_row_hash(previous, payload)
        if record.get("prev_hash") != previous:
            raise ResolutionChainBrokenError(
                session_ref,
                int(record["event_seq"]),
                f"prev_hash is {record.get('prev_hash')!r}, expected {previous!r}",
            )
        if record["row_hash"] != expected:
            raise ResolutionChainBrokenError(
                session_ref,
                int(record["event_seq"]),
                f"row_hash is {record['row_hash']!r}, recomputed {expected!r} -- this "
                "record's contents were edited after it was written",
            )
        previous = str(record["row_hash"])


async def _existing_source_keys(tenant_id: uuid.UUID) -> set[str]:
    async with tenant_scope(tenant_id) as session:
        return set(
            (
                await session.execute(
                    select(KnowledgeSource.key).where(KnowledgeSource.tenant_id == tenant_id)
                )
            ).scalars()
        )


def _fork_key(key: str, taken: set[str]) -> str:
    """``foo`` -> ``foo-imported`` -> ``foo-imported-2`` ... Never returns a key already in
    ``taken``, so the caller cannot accidentally overwrite by reusing the result."""
    candidate = f"{key}-imported"
    suffix = 2
    while candidate in taken:
        candidate = f"{key}-imported-{suffix}"
        suffix += 1
    return candidate


_ADOPTABLE_WORKSPACE_SETTINGS = ("secret_mode", "conduct_rules")


async def _adopt_workspace_settings(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, files: dict[str, bytes], report: ImportReport
) -> None:
    """Adopt the bundle's workspace settings the target has no opinion on yet.

    The bundle has always carried ``workspace.json`` and nothing ever read it. That lost
    exactly the settings a sample depends on: ``secret_mode`` (without it an imported
    mystery ran in the leak-proof default and the whole cast played with nothing to
    hide) and ``conduct_rules``. Additive only -- a key the target workspace already set
    is never overwritten, so importing a bundle cannot silently reconfigure a workspace
    someone tuned.
    """
    raw = files.get("workspace.json")
    if raw is None:
        return
    try:
        source_settings = dict(json.loads(raw.decode()).get("settings") or {})
    except (ValueError, AttributeError):
        return
    from core.tenancy.models import Workspace

    async with tenant_scope(tenant_id) as session:
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None:
            return
        settings = dict(workspace.settings)
        adopted = [
            key
            for key in _ADOPTABLE_WORKSPACE_SETTINGS
            if key in source_settings and key not in settings
        ]
        for key in adopted:
            settings[key] = source_settings[key]
        if adopted:
            workspace.settings = settings
            report.imported.append(f"workspace settings: {', '.join(adopted)}")


async def ensure_workflow_for_bundle(
    tenant_id: uuid.UUID, manifest: dict[str, Any], agent_records: list[dict[str, Any]]
) -> str:
    """Pin the workflow this bundle's content depends on, so its pack's axes exist.

    Behaviour profiles reference axes by pack id ("rpg_v1"), and a tenant only has those
    rows once the owning workflow has been selected -- selecting one is what materializes
    its pack. Import into a tenant sitting on a different workflow therefore dropped every
    slider on every imported persona, quietly, because the profile write failed and was
    swallowed.

    Newer bundles name the workflow outright. Older ones do not, so it is recovered from
    the pack ids the profiles reference ("rpg_v1" -> "rpg"), and only applied when a
    workflow by that name actually exists for this tenant -- a guess that cannot be
    verified is not acted on. Returns the key applied, or "" when none was.
    """
    from core.workflows.service import (
        get_tenant_workflow_key,
        get_workflow_for_tenant,
        set_tenant_workflow,
    )

    wanted = str(manifest.get("workflow_key") or "").strip()
    if not wanted:
        packs = {
            str(v.get("pack_id") or "")
            for record in agent_records
            for v in record.get("behavior_profile_versions") or []
        }
        candidates = {re.sub(r"_v\d+$", "", p) for p in packs if p}
        candidates.discard("")
        # Only unambiguous recovery: two different packs mean no single right answer.
        wanted = next(iter(candidates)) if len(candidates) == 1 else ""
    if not wanted:
        return ""
    if await get_tenant_workflow_key(tenant_id) == wanted:
        return wanted
    if await get_workflow_for_tenant(tenant_id, wanted) is None:
        return ""
    await set_tenant_workflow(tenant_id, wanted)
    return wanted


async def import_bundle(
    data: bytes,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    bundle_ref: str,
    password: str | None = None,
    encryptor: Encryptor | None = None,
    importing_principal_id: uuid.UUID | None = None,
    permission_service: PermissionService | None = None,
    moderation_provider: ModerationProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    sections: frozenset[str] | None = None,
) -> ImportReport:
    """The whole import, in order: open, verify, upcast, verify chains, then write.

    Verification happens before *any* row is written -- not per-object as it goes. A
    half-imported bundle whose second half failed verification is worse than a refused
    one, because it leaves a tenant with content nobody chose and no obvious way to tell
    which rows came from where.

    ``sections`` limits what lands; ``None`` imports everything, which is what every
    caller before this did and still means. It exists because import is additive by
    design -- colliding keys fork rather than overwrite -- and that property, which makes
    a first import safe, is exactly what doubles a workspace on the second one. Being
    able to say "just the personas" is the difference between re-importing an updated
    bundle and rebuilding a workspace by hand. Verification still covers the whole file:
    a bundle is accepted or refused as a unit, and choosing part of it is not a reason to
    check less of it.
    """
    wanted = None if sections is None else frozenset(sections)

    def _do(section: str) -> bool:
        return wanted is None or section in wanted

    from core.portability.crypto import decrypt_bundle, is_encrypted

    if is_encrypted(data):
        data = decrypt_bundle(data, password or "")

    reader = open_bundle(data)
    broken = verify_bundle(reader)
    if broken:
        raise BundleIntegrityError(
            f"{len(broken)} file(s) do not match the manifest's integrity map: {broken}"
        )

    files, manifest = upcast_to_current(dict(reader.files), dict(reader.manifest))
    tenant_ref = str(manifest["tenant_ref"])

    session_prefixes = sorted(
        {p.split("/")[1] for p in files if p.startswith("sessions/") and "/" in p[9:]}
    )
    for prefix in session_prefixes:
        session_ref = prefix.removeprefix("session_")
        path = f"sessions/{prefix}/resolutions.jsonl"
        if path not in files:
            continue
        records = [_json(line) for line in files[path].decode().splitlines() if line.strip()]
        verify_resolution_chain(records, tenant_ref, session_ref)

    report = ImportReport()
    await _adopt_workspace_settings(tenant_id, workspace_id, files, report)
    if _do("knowledge"):
        await _import_knowledge(files, tenant_id, workspace_id, report, bundle_ref=bundle_ref)
    if _do("entities"):
        await _import_schemas_and_entities(
            files, tenant_id, workspace_id, report, bundle_ref=bundle_ref
        )
    if _do("sessions"):
        await _import_sessions(files, tenant_id, workspace_id, report)
    if _do("personas"):
        await _import_personas(
            files, tenant_id, workspace_id, report, encryptor=encryptor, manifest=manifest
        )
    # After personas (their principals are what a band's membership resolves to) and
    # before nothing in particular -- knowledge already landed carrying its scope_key.
    #
    # A band whose members were not imported resolves to nobody, which is why scopes
    # follow personas rather than standing alone: deselecting personas and keeping scopes
    # produces empty bands, not an error. The UI says so; the importer does not guess.
    if _do("scopes"):
        await _import_scopes(files, tenant_id, workspace_id, report)
    if _do("flows"):
        await _import_process(files, tenant_id, workspace_id, report)
    # Mechanics after flows, for the same reason they export after them: a flow names the
    # tools, and a tool names its rule system.
    if _do("rules"):
        await _import_rules(files, tenant_id, workspace_id, report)
    if _do("vocabulary"):
        await _import_vocabulary(files, tenant_id, workspace_id, report)
    # Last, and deliberately: a secret is attached to a persona or an entity and held by
    # a persona, so everything it points at has to exist before it can be resolved --
    # which is also why it is skipped when either was left out rather than importing a
    # secret that can never be disclosed to anyone.
    if _do("secrets") and _do("personas"):
        await _import_secrets(
            files,
            tenant_id,
            workspace_id,
            report,
            encryptor=encryptor,
            importing_principal_id=importing_principal_id,
            permission_service=permission_service,
            moderation_provider=moderation_provider,
            embedding_provider=embedding_provider,
        )
    return report


PLACEHOLDER_CONNECTION_NAME = "missing-connection"


async def _placeholder_connection(tenant_id: uuid.UUID, encryptor: Encryptor) -> uuid.UUID:
    """Personas travel without credentials by design, so an imported persona may point
    at a connection that never came along. It binds to this per-tenant placeholder
    (provider 'none') until someone assigns a real one -- the personas view warns on
    it. Better an actionable warning than a refused import or a null FK migration."""
    from sqlalchemy import select as sa_select

    from core.agents.authoring import create_agent
    from core.agents.models import Agent
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(tenant_id) as session:
        existing = (
            (
                await session.execute(
                    sa_select(Agent).where(
                        Agent.name == PLACEHOLDER_CONNECTION_NAME, Agent.archived_at.is_(None)
                    )
                )
            )
            .scalars()
            .first()
        )
    if existing is not None:
        return existing.id
    row = await create_agent(
        tenant_id,
        PLACEHOLDER_CONNECTION_NAME,
        "none",
        "unconfigured",
        encryptor=encryptor,
    )
    return row.id


async def _import_personas(
    files: dict[str, bytes],
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    report: ImportReport,
    *,
    encryptor: Encryptor | None,
    manifest: dict[str, Any] | None = None,
) -> None:
    """agents/ + (optionally) connections/: the half of the bundle the importer used
    to silently drop. Connections that travelled (encrypted export) are recreated with
    their credentials sealed by THIS deployment's encryptor; personas whose connection
    did not travel bind to the placeholder and surface a warning in the UI."""
    persona_paths = sorted(p for p in files if p.startswith("agents/") and p.endswith(".json"))
    if not persona_paths or encryptor is None:
        return

    # Pin the workflow these personas were authored under BEFORE writing any profile:
    # selecting it is what materializes the pack's axes, and a profile referencing axes
    # that do not exist yet is discarded.
    agent_records = [_json(files[path].decode()) for path in persona_paths]
    applied = await ensure_workflow_for_bundle(tenant_id, manifest or {}, agent_records)
    if applied:
        report.imported.append(f"workflow:{applied} (pinned for imported behaviour axes)")

    from sqlalchemy import select as sa_select

    from core.agents.authoring import create_agent, create_persona
    from core.agents.models import Agent, Persona
    from core.behavior.repo import create_behavior_profile
    from core.tenancy.scope import tenant_scope

    # 1) connections that travelled, recreated (dedupe by name)
    connection_map: dict[str, uuid.UUID] = {}  # exported connection id -> local id
    if "connections/connections.json" in files:
        records = json.loads(files["connections/connections.json"].decode())
        async with tenant_scope(tenant_id) as session:
            existing_by_name = {
                a.name: a.id
                for a in (
                    await session.execute(sa_select(Agent).where(Agent.archived_at.is_(None)))
                ).scalars()
            }
        for record in records:
            if record["name"] in existing_by_name:
                connection_map[record["id"]] = existing_by_name[record["name"]]
                continue
            row = await create_agent(
                tenant_id,
                record["name"],
                record["provider"],
                record["model"],
                params=record.get("params") or None,
                api_key=record.get("api_key") or None,
                api_base=record.get("api_base"),
                encryptor=encryptor,
            )
            connection_map[record["id"]] = row.id
            report.imported.append(f"connection:{record['name']}")

    placeholder_id: uuid.UUID | None = None
    async with tenant_scope(tenant_id) as session:
        existing_by_key = {
            p.key: (p.id, p.principal_id)
            for p in (
                await session.execute(
                    sa_select(Persona).where(
                        Persona.workspace_id == workspace_id, Persona.archived_at.is_(None)
                    )
                )
            ).scalars()
        }

    for path in persona_paths:
        record = json.loads(files[path].decode())
        if record["key"] in existing_by_key:
            # Skipped, but still MAPPED: later sections resolve their subjects through
            # id_map, so a re-import that skips a resident persona must still let that
            # persona receive what the first pass could not attach. Without this, an
            # import whose secrets were refused (observed live: the importer lacked the
            # workspace membership, every create_secret was denied, and the whole cast
            # played a mystery with nothing to hide) could never be repaired by
            # re-importing -- the secrets' subjects "did not come with the bundle".
            existing_id, existing_principal = existing_by_key[record["key"]]
            report.id_map[str(record["id"])] = str(existing_id)
            report.persona_principals[str(record["id"])] = existing_principal
            report.skipped.append(f"persona:{record['key']} (key exists; mapped to resident)")
            continue
        connection_id = connection_map.get(record.get("model_profile_ref") or "")
        if connection_id is None:
            if placeholder_id is None:
                placeholder_id = await _placeholder_connection(tenant_id, encryptor)
            connection_id = placeholder_id
            report.skipped.append(
                f"persona:{record['key']} bound to placeholder (connection did not travel)"
            )
        persona = await create_persona(
            tenant_id,
            workspace_id,
            record["key"],
            record["name"],
            connection_id,
            persona_type=record.get("persona_type", "participant"),
            persona_md=record.get("persona_md", ""),
            params=dict(record.get("params") or {}),
        )
        report.id_map[str(record["id"])] = str(persona.id)
        report.persona_principals[str(record["id"])] = persona.principal_id
        report.imported.append(f"persona:{record['key']}")
        for version in record.get("behavior_profile_versions", []):
            try:
                await create_behavior_profile(
                    tenant_id,
                    persona.id,
                    version["pack_id"],
                    {k: int(v) for k, v in (version.get("axis_values") or {}).items()},
                )
            except Exception:  # noqa: BLE001 -- advisory content must not fail an import
                # Name the pack. "(axes not loaded)" sent people looking at the persona,
                # when the cause is that this deployment has no workflow providing that
                # pack -- usually its plugin repository was unreachable at install.
                report.skipped.append(
                    f"behavior:{record['key']} v{version.get('version')} "
                    f"(no axes for pack {version.get('pack_id')!r} in this tenant -- "
                    f"select the workflow that provides them, then re-import)"
                )
                break


def _json(line: str) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(line)
    return parsed


async def _import_knowledge(
    files: dict[str, bytes],
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    report: ImportReport,
    *,
    bundle_ref: str,
) -> None:
    """Sources, their version history, and their entries -- with new ids throughout, so an
    import can never collide with, or silently join onto, a resident object that happens to
    share an id. Entry bodies run through the injection scan on the way in."""
    taken = await _existing_source_keys(tenant_id)

    for meta_path in sorted(p for p in files if p.endswith("/meta.json") and "/entries/" not in p):
        if not meta_path.startswith("knowledge/"):
            continue
        source_meta = json.loads(files[meta_path].decode())
        prefix = meta_path.removesuffix("/meta.json")
        original_key = str(source_meta["key"])
        key = original_key
        if key in taken:
            key = _fork_key(key, taken)
            report.forked_keys.append((original_key, key))
        taken.add(key)

        async with tenant_scope(tenant_id) as session:
            source = KnowledgeSource(
                tenant_id=tenant_id,
                key=key,
                name=str(source_meta["name"]),
                class_=str(source_meta["class"]),
                visibility=str(source_meta.get("visibility", "tenant")),
            )
            session.add(source)
            await session.flush()
            source_id = source.id
        report.knowledge_sources += 1
        report.id_map[str(source_meta["id"])] = str(source_id)

        version_paths = sorted(p for p in files if p.startswith(f"{prefix}/versions/"))
        for version_path in version_paths:
            version_meta = json.loads(files[version_path].decode())
            version_label = version_path.rsplit("/", 1)[-1].removesuffix(".json")
            async with tenant_scope(tenant_id) as session:
                version = KnowledgeSourceVersion(
                    tenant_id=tenant_id,
                    knowledge_source_id=source_id,
                    version_number=int(version_meta["version_number"]),
                    content_hash=str(version_meta["content_hash"]),
                    change_note=f"imported from {bundle_ref}",
                )
                session.add(version)
                await session.flush()
                version_id = version.id
                # Point the source at what was just imported, the way publish_version
                # does. Without it the entries exist, are published and retrieve fine
                # (the workspace attachment pins the version directly), but the source
                # reads as EMPTY everywhere that resolves through current_version_id --
                # which is the authoring UI. An imported handbook you cannot open is
                # indistinguishable from one that failed to import.
                imported_source = await session.get(KnowledgeSource, source_id)
                if imported_source is not None:
                    imported_source.current_version_id = version_id
            report.id_map[str(version_meta["id"])] = str(version_id)

            entry_prefix = f"{prefix}/entries/{version_label}/"
            for body_path in sorted(
                p for p in files if p.startswith(entry_prefix) and p.endswith(".md")
            ):
                meta_json_path = body_path.removesuffix(".md") + ".meta.json"
                if meta_json_path not in files:
                    continue
                entry_meta = json.loads(files[meta_json_path].decode())
                body = files[body_path].decode()

                reason = quarantine_reason(scan_text(f"{entry_meta['title']}\n{body}"))
                async with tenant_scope(tenant_id) as session:
                    entry = KnowledgeEntry(
                        tenant_id=tenant_id,
                        knowledge_source_id=source_id,
                        version_id=version_id,
                        entry_key=str(entry_meta["entry_key"]),
                        title=str(entry_meta["title"]),
                        body_md=body,
                        class_=str(entry_meta["class"]),
                        scope_key=str(entry_meta["scope_key"]),
                        keys=list(entry_meta.get("keys", [])),
                        secondary_keys=list(entry_meta.get("secondary_keys", [])),
                        logic=str(entry_meta.get("logic", "AND")),
                        use_regex=bool(entry_meta.get("use_regex", False)),
                        constant=bool(entry_meta.get("constant", False)),
                        sticky=entry_meta.get("sticky"),
                        cooldown=entry_meta.get("cooldown"),
                        delay=entry_meta.get("delay"),
                        trigger_pct=entry_meta.get("trigger_pct"),
                        inclusion_group=entry_meta.get("inclusion_group"),
                        position=str(entry_meta.get("position", "before_char")),
                        insertion_order=int(entry_meta.get("insertion_order", 0)),
                        quarantined=reason is not None,
                        quarantine_reason=reason,
                    )
                    session.add(entry)
                    await session.flush()
                    entry_id = entry.id
                report.entries += 1
                report.id_map[str(entry_meta["id"])] = str(entry_id)
                if reason is not None:
                    report.quarantined_entries.append((str(entry_meta["entry_key"]), reason))

                await _import_chunks_for_entry(
                    files, tenant_id, prefix, str(entry_meta["id"]), entry_id, version_id, reason
                )

        # Attach the source to the target workspace. Without this the entries exist in the
        # tenant and are attached to nothing, so no retrieval ever reaches them: the
        # bundle's handbook would be present in the database and absent from every agent's
        # context -- the failure looks like a model that ignored its briefing.
        await _attach_imported_source(files, tenant_id, workspace_id, prefix, source_id, report)


async def _attach_imported_source(
    files: dict[str, bytes],
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    prefix: str,
    source_id: uuid.UUID,
    report: ImportReport,
) -> None:
    """Re-create the exported workspace attachment against the importing workspace.

    The exported ``workspace_id`` is meaningless here -- it names a workspace in another
    tenant -- so the attachment lands on the workspace being imported INTO, which is the
    only one the caller asked about. ``version_pin`` is remapped through the id map; a pin
    that did not travel becomes an unpinned attachment (follow the latest version) rather
    than a dangling reference."""
    from core.knowledge.authoring import attach_source_to_workspace

    path = f"{prefix}/attachments.json"
    if path not in files:
        return
    records = json.loads(files[path].decode())
    if not records:
        return
    record = records[0]
    pinned = report.id_map.get(str(record.get("version_pin")))
    await attach_source_to_workspace(
        tenant_id,
        workspace_id,
        source_id,
        str(record.get("scope_key") or "workspace_public"),
        priority_weight=float(record.get("priority_weight") or 1.0),
        version_pin=uuid.UUID(pinned) if pinned else None,
        enabled=bool(record.get("enabled", True)),
    )
    report.imported.append(f"knowledge attachment:{prefix.split('/')[-1]}")


async def _import_chunks_for_entry(
    files: dict[str, bytes],
    tenant_id: uuid.UUID,
    prefix: str,
    original_entry_id: str,
    entry_id: uuid.UUID,
    version_id: uuid.UUID,
    reason: str | None,
) -> None:
    """A chunk inherits its entry's quarantine verdict. It has to: retrieval filters on the
    *chunk's* flag (a pushdown, not a join condition every retrieval path must remember),
    so a quarantined entry whose chunks weren't flagged would be quarantined in name only."""
    for chunk_path in sorted(p for p in files if p.startswith(f"{prefix}/chunks/")):
        chunk = json.loads(files[chunk_path].decode())
        if str(chunk.get("entry_id")) != original_entry_id:
            continue
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO knowledge_chunk "
                    "(tenant_id, entry_id, version_id, ordinal, text, token_count, class, "
                    " scope_key, embedding, content_hash, quarantined) "
                    "VALUES (:tenant_id, :entry_id, :version_id, :ordinal, :text, "
                    " :token_count, :class_, :scope_key, NULL, md5(:text), :quarantined)"
                ),
                {
                    "tenant_id": tenant_id,
                    "entry_id": entry_id,
                    "version_id": version_id,
                    "ordinal": int(chunk.get("ordinal", 0)),
                    "text": str(chunk["text"]),
                    "token_count": int(chunk.get("token_count", 0)),
                    "class_": str(chunk["class"]),
                    "scope_key": str(chunk["scope_key"]),
                    "quarantined": reason is not None,
                },
            )


async def approve_quarantined_entry(
    tenant_id: uuid.UUID, entry_id: uuid.UUID, reviewer_principal_id: uuid.UUID
) -> None:
    """A human clears one entry: the flag drops on the entry *and* its chunks, in one
    transaction, and the reviewer is recorded. One transaction because a half-cleared entry
    -- visible in the review queue as approved, still invisible to retrieval -- is the kind
    of state that gets diagnosed as "search is broken"."""
    async with tenant_scope(tenant_id) as session:
        entry = await session.get(KnowledgeEntry, entry_id)
        if entry is None:
            raise ValueError(f"no knowledge entry {entry_id} in this tenant")
        entry.quarantined = False
        entry.quarantine_reviewed_by = reviewer_principal_id
        entry.quarantine_reviewed_at = datetime.now(UTC)
        await session.execute(
            text(
                "UPDATE knowledge_chunk SET quarantined = false "
                "WHERE tenant_id = :tenant_id AND entry_id = :entry_id"
            ),
            {"tenant_id": tenant_id, "entry_id": entry_id},
        )


async def list_quarantined_entries(tenant_id: uuid.UUID) -> list[KnowledgeEntry]:
    """The review queue. Ordered oldest first -- a queue a reviewer works from the top of
    should hand them the thing that has been waiting longest."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(KnowledgeEntry)
                .where(KnowledgeEntry.quarantined.is_(True))
                .order_by(KnowledgeEntry.created_at)
            )
        ).scalars()
        return list(rows)


async def _import_schemas_and_entities(
    files: dict[str, bytes],
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    report: ImportReport,
    *,
    bundle_ref: str,
) -> None:
    """Schemas first (an entity needs its shape), then instances -- each with a new id, and
    each recording an ``entity_state_change`` with ``cause='import'`` and the bundle as
    ``cause_ref``. That row is not bookkeeping: it is the only thing that later explains
    why an entity in this tenant holds state nobody here ever set."""
    schema_map: dict[str, uuid.UUID] = {}
    for path in sorted(p for p in files if p.startswith("schemas/") and p.endswith(".json")):
        payload = json.loads(files[path].decode())
        definition = EntitySchemaDefinition.model_validate(payload["definition"])
        row = await save_schema(
            tenant_id,
            workspace_id,
            _forked_entity_key(str(payload["key"]), report),
            int(payload["version"]),
            definition,
        )
        schema_map[str(payload["id"])] = row.id
        report.id_map[str(payload["id"])] = str(row.id)

    for path in sorted(p for p in files if p.startswith("entities/") and p.endswith(".json")):
        payload = json.loads(files[path].decode())
        schema_id = schema_map.get(str(payload["schema_id"]))
        if schema_id is None:
            # An entity whose schema the exporter's visibility withheld. Skipping is the
            # only honest option: importing it against a guessed schema would fabricate a
            # shape, and validation would then pass or fail for invented reasons.
            continue
        definition_row = await get_schema(tenant_id, schema_id)
        assert definition_row is not None
        entity = await create_entity(
            tenant_id,
            workspace_id,
            schema_id,
            definition_row.to_definition(),
            key=f"{payload['key']}-imported-{uuid.uuid4().hex[:6]}",
            name=str(payload["name"]),
            scope_key=str(payload["scope_key"]),
            data=dict(payload["data"]),
        )
        report.id_map[str(payload["id"])] = str(entity.id)
        async with tenant_scope(tenant_id) as session:
            for field_path, new_value in dict(payload["data"]).items():
                session.add(
                    EntityStateChangeRow(
                        tenant_id=tenant_id,
                        entity_id=entity.id,
                        session_id=None,
                        event_seq=None,
                        field_path=field_path,
                        old_value=None,
                        new_value=new_value,
                        cause="import",
                        cause_ref=bundle_ref,
                    )
                )


def _forked_entity_key(key: str, report: ImportReport) -> str:
    """Schema keys fork on the same principle source keys do -- ``save_schema`` versions by
    ``(workspace, key)``, so reusing a resident key would silently graft imported shapes
    onto a resident schema's version history."""
    forked = f"{key}-imported"
    report.forked_keys.append((key, forked))
    return forked


async def _import_sessions(
    files: dict[str, bytes],
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    report: ImportReport,
) -> None:
    """Sessions with their full logs. The agent is *not* remapped to a local one: an
    imported session references the agent id it recorded, and inventing a local agent to
    point it at would claim an agent in this tenant took turns it never took. The session
    imports with ``status='imported'`` for the same reason -- it is history, not something
    to resume by accident."""
    prefixes = sorted(
        {p.split("/")[1] for p in files if p.startswith("sessions/") and "/" in p[9:]}
    )
    for prefix in prefixes:
        meta_path = f"sessions/{prefix}/meta.json"
        if meta_path not in files:
            continue
        meta = json.loads(files[meta_path].decode())
        persona_id = await _any_local_agent(tenant_id, workspace_id)
        if persona_id is None:
            continue

        async with tenant_scope(tenant_id) as session:
            row = SessionRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                persona_id=persona_id,
                current_phase=str(meta["current_phase"]),
                status="imported",
                state=dict(meta["state"]),
            )
            session.add(row)
            await session.flush()
            session_id = row.id
        report.sessions += 1
        report.id_map[str(meta["id"])] = str(session_id)

        events = _read_jsonl(files, f"sessions/{prefix}/events.jsonl")
        async with tenant_scope(tenant_id) as session:
            for event in events:
                session.add(
                    SessionEventRow(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        event_seq=int(event["event_seq"]),
                        kind=str(event["kind"]),
                        payload=dict(event["payload"]),
                    )
                )
            refreshed = await session.get(SessionRow, session_id)
            assert refreshed is not None
            refreshed.next_event_seq = max((int(e["event_seq"]) for e in events), default=-1) + 1
        report.events += len(events)

        checkpoints = _read_jsonl(files, f"sessions/{prefix}/checkpoints.jsonl")
        async with tenant_scope(tenant_id) as session:
            for checkpoint in checkpoints:
                session.add(
                    CheckpointRow(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        event_seq=int(checkpoint["event_seq"]),
                        phase=str(checkpoint["phase"]),
                        state=dict(checkpoint["state"]),
                        actor_cursor=dict(checkpoint["actor_cursor"]),
                        entity_versions=dict(checkpoint["entity_versions"]),
                        knowledge_version_pins=dict(checkpoint["knowledge_version_pins"]),
                    )
                )

        report.resolutions += len(_read_jsonl(files, f"sessions/{prefix}/resolutions.jsonl"))


async def _any_local_agent(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> uuid.UUID | None:
    async with tenant_scope(tenant_id) as session:
        persona_id: uuid.UUID | None = await session.scalar(
            select(Persona.id).where(Persona.workspace_id == workspace_id).limit(1)
        )
        return persona_id


def _read_jsonl(files: dict[str, bytes], path: str) -> list[dict[str, Any]]:
    if path not in files:
        return []
    return [_json(line) for line in files[path].decode().splitlines() if line.strip()]


# ── process, vocabulary, secrets ─────────────────────────────────────────────────────
# The sections export has always written and import used to walk past. A bundle whose
# flow, vocabulary and secrets evaporate on arrival is not a portable workspace: it is a
# pile of knowledge entries with no way to run them.


async def _import_scopes(
    files: dict[str, bytes],
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    report: ImportReport,
) -> None:
    """Recreate the bundle's group scope bands -- the "levels of lore".

    Knowledge arrives already carrying its ``scope_key``; without the band itself that
    knowledge points at a scope that does not exist and fails closed, unreachable by
    everyone including the personas it was written for. Members travel as persona keys
    and are resolved to THIS workspace's principals, because a principal id from the
    exporting tenant means nothing here.

    Additive and idempotent: a band the target workspace already defines is left exactly
    as it is (re-importing must not quietly widen who can read a band), and a member
    whose persona did not travel is skipped rather than invented.
    """
    paths = sorted(p for p in files if p.startswith("scopes/") and p.endswith(".json"))
    if not paths:
        return

    from sqlalchemy import select as sa_select

    from core.agents.models import Persona
    from core.assembler.models import ScopeRow
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(tenant_id) as session:
        personas = list(
            (
                await session.execute(
                    sa_select(Persona).where(Persona.workspace_id == workspace_id)
                )
            ).scalars()
        )
        principal_by_key = {p.key: str(p.principal_id) for p in personas}
        existing = set(
            (
                await session.execute(
                    sa_select(ScopeRow.key).where(ScopeRow.workspace_id == workspace_id)
                )
            ).scalars()
        )

        for path in paths:
            record = _json(files[path].decode())
            key = str(record.get("key") or "").strip()
            if not key or key in existing:
                continue
            principal_ids = [
                principal_by_key[k]
                for k in (record.get("persona_keys") or [])
                if k in principal_by_key
            ]
            session.add(
                ScopeRow(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    key=key,
                    kind=str(record.get("kind") or "group"),
                    members={
                        "principal_ids": principal_ids,
                        "roles": list(record.get("roles") or []),
                    },
                )
            )
            existing.add(key)
            report.imported.append(f"scope:{key} ({len(principal_ids)} member(s))")


async def _import_rules(
    files: dict[str, bytes],
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    report: ImportReport,
) -> None:
    """Rule systems and the tool definitions that bind them.

    Both are upserts by ``(tenant_id, key)`` rather than forks, and that is a deliberate
    departure from everything else in this module. A forked ruleset is not a second
    opinion, it is a broken one: a tool names its rule system by ``validation_ref``, a
    string key, so a `basic_fantasy-imported` row would be a system nothing can reach
    while the tool that meant to reach it silently validates against whatever holds the
    original name. Forking here would produce exactly the quiet wrong answer the rest of
    the forking policy exists to avoid.

    That trade is safe in a way the content sections are not: mechanics are arithmetic --
    dice grammar, check types, modifier expressions -- with no authored prose to lose and
    nothing scope-bearing to leak. Re-importing a sample updates its mechanics in place,
    which is what someone re-importing a sample means.

    ``impl_ref`` is carried but never resolved to anything: core.resolution.registry is
    explicit that it is descriptive, and dispatch is by tool key against handlers the
    composition root registered. A bundle cannot introduce behaviour here, only name
    behaviour that already exists -- which is what makes it safe for a .pyr to carry a
    tool at all.
    """
    from core.resolution.registry import ToolDefinitionSchema, register_tool_definition
    from core.resolution.rule_system import RuleSystemDefinitionSchema, create_rule_system

    for path in sorted(files):
        if not path.startswith("rules/rule_system_") or not path.endswith(".json"):
            continue
        try:
            definition = RuleSystemDefinitionSchema.model_validate(_json(files[path]))
        except Exception as exc:  # noqa: BLE001 -- a bad ruleset is content, not tampering
            report.skipped.append(f"rule system {path}: {str(exc)[:160]}")
            continue
        await create_rule_system(tenant_id, definition)
        report.imported.append(f"rule system: {definition.key}")

    # Tools after rule systems: a tool's validation_ref names one, and landing the tool
    # first would leave a window where it points at nothing.
    for path in sorted(files):
        if not path.startswith("rules/tool_") or not path.endswith(".json"):
            continue
        try:
            tool = ToolDefinitionSchema.model_validate(_json(files[path]))
        except Exception as exc:  # noqa: BLE001
            report.skipped.append(f"tool {path}: {str(exc)[:160]}")
            continue
        await register_tool_definition(tenant_id, tool)
        report.imported.append(f"tool: {tool.key}")


async def _import_process(
    files: dict[str, bytes],
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    report: ImportReport,
) -> None:
    """Flow definitions. Colliding keys fork, like everything else here -- a definition a
    session is pinned to must never be replaced by an incoming one.

    A definition that fails validation is *reported and skipped*, not fatal. Integrity
    failure means tampering and refuses the whole bundle; a flow this deployment cannot
    validate (an older build, a feature it lacks) is a content problem, and losing the
    knowledge and personas over it would help nobody."""
    from core.process.authoring import (
        DefinitionValidationError,
        create_definition,
        list_definitions,
    )

    paths = sorted(p for p in files if p.startswith("process/") and p.endswith(".json"))
    if not paths:
        return

    # Scoped to the target workspace, because that is where the uniqueness lives
    # (`uq_process_definition_workspace_key_version`). Forking against every key in the
    # tenant would rename a flow on the way into an empty workspace, for no collision.
    taken = {
        row.key
        for row in await list_definitions(
            tenant_id, workspace_id=workspace_id, include_archived=True
        )
    }
    for path in paths:
        record = json.loads(files[path].decode())
        key = str(record["key"])
        if key in taken:
            forked = _fork_key(key, taken)
            report.forked_keys.append((key, forked))
            key = forked
        taken.add(key)
        try:
            row = await create_definition(
                tenant_id,
                key,
                str(record.get("name") or key),
                dict(record.get("definition") or {}),
                workspace_id=workspace_id,
            )
        except DefinitionValidationError as exc:
            report.skipped.append(f"process:{key} (does not validate here: {exc})")
            continue
        report.id_map[str(record["id"])] = str(row.id)
        report.imported.append(f"process:{key}")


async def _import_vocabulary(
    files: dict[str, bytes],
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    report: ImportReport,
) -> None:
    """The workspace's label overlay.

    An overlay whose key already resolves here is *bound*, not copied: export writes
    whatever the workspace resolved to, which is usually one of the deployment's own
    system overlays, and cloning `rpg_v1` into a tenant-owned duplicate on every import
    would be both wrong and unbounded. Only a key this deployment does not have becomes a
    new tenant overlay."""
    from core.vocabulary.service import get_overlay_by_key, set_workspace_overlay, upsert_overlay

    paths = sorted(p for p in files if p.startswith("vocabulary/") and p.endswith(".json"))
    if not paths:
        return

    record = json.loads(files[paths[0]].decode())
    key = str(record["key"])
    existing = await get_overlay_by_key(tenant_id, key)
    if existing is not None:
        await set_workspace_overlay(tenant_id, workspace_id, existing.id)
        report.imported.append(f"vocabulary:{key} (bound to the one already here)")
        return

    overlay = await upsert_overlay(
        tenant_id, key, str(record.get("name") or key), dict(record.get("labels") or {})
    )
    await set_workspace_overlay(tenant_id, workspace_id, overlay.id)
    report.id_map[str(record["id"])] = str(overlay.id)
    report.imported.append(f"vocabulary:{key}")


async def _import_secrets(
    files: dict[str, bytes],
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    report: ImportReport,
    *,
    encryptor: Encryptor | None,
    importing_principal_id: uuid.UUID | None,
    permission_service: PermissionService | None,
    moderation_provider: ModerationProvider | None,
    embedding_provider: EmbeddingProvider | None = None,
) -> None:
    """Secrets, and who holds them.

    Written through ``secrets.authoring.create_secret`` rather than as rows, for three
    reasons that all matter here: the importing principal must actually be entitled to
    author secrets in the target workspace, the content is moderated exactly like
    authored content (a bundle is attacker-controlled input, and this content is headed
    for an agent's context), and each secret gets its ``secret:create`` audit row in the
    same transaction.

    **A secret with no content is skipped, not stubbed.** A sanitised export -- or a
    participant export by someone who did not hold it -- carries the gist and nothing
    else. Importing that as a row with empty content would put a secret in front of the
    disclosure gate that has nothing to conceal: present, plausible, and hollow. Saying it
    did not come is the honest outcome, and the report names each one.

    **The gist embedding is recomputed here**, and it is not optional in practice. A
    bundle carries the gist text but not its vector (embeddings are per-deployment), and
    the disclosure gate drops any candidate whose ``gist_embedding`` is NULL -- silently,
    with no decision row and no warning. An imported secret without one is therefore
    inert: held by the right persona, visible in the UI, and never once considered by the
    gate. If no embedder is supplied the secret still imports, and the report says the
    gate will not see it until the gist is re-embedded.
    """
    paths = sorted(p for p in files if p.startswith("secrets/") and p.endswith(".json"))
    if not paths:
        return
    if (
        encryptor is None
        or importing_principal_id is None
        or permission_service is None
        or moderation_provider is None
    ):
        report.skipped.append(
            f"secrets:{len(paths)} (this import path supplies no authoring principal)"
        )
        return

    from core.secrets.authoring import (
        SecretAccessDeniedError,
        SecretContentRejectedError,
        add_holder,
        create_secret,
    )

    # subject_kind -> where its id was remapped to. 'workspace' is the target workspace
    # itself; the rest have to have arrived in this same bundle to be resolvable.
    for path in paths:
        record = json.loads(files[path].decode())
        ref = str(record.get("id"))
        content = record.get("content")
        if not content:
            report.skipped.append(f"secret:{ref} (no content in this bundle)")
            continue

        subject_kind = str(record.get("subject_kind") or "")
        if subject_kind == "workspace":
            subject_id: uuid.UUID | None = workspace_id
        else:
            mapped = report.id_map.get(str(record.get("subject_id")))
            subject_id = uuid.UUID(mapped) if mapped else None
        if subject_id is None:
            report.skipped.append(f"secret:{ref} (its {subject_kind} did not come with the bundle)")
            continue

        # A healing re-import must not double the secrets that DID land the first time:
        # the same subject holding the same gist is the same secret.
        async with tenant_scope(tenant_id) as session:
            from core.secrets.models import SecretRow

            duplicate = await session.scalar(
                select(SecretRow.id).where(
                    SecretRow.workspace_id == workspace_id,
                    SecretRow.subject_id == subject_id,
                    SecretRow.gist == str(record.get("gist") or ""),
                )
            )
        if duplicate is not None:
            report.skipped.append(f"secret:{ref} (already present)")
            continue

        try:
            row = await create_secret(
                tenant_id,
                workspace_id,
                importing_principal_id,
                subject_kind=subject_kind,
                subject_id=subject_id,
                content=str(content),
                gist=str(record.get("gist") or ""),
                scope_key=str(record.get("scope_key") or "workspace_public"),
                encryptor=encryptor,
                permission_service=permission_service,
                moderation_provider=moderation_provider,
                hint_text=record.get("hint_text"),
                behavioral_directive=record.get("behavioral_directive"),
                # Travels with the content, and falls back closed: a bundle written
                # before this field existed carries guarded secrets, not publishable ones.
                publication=str(record.get("publication") or "guarded"),
            )
        except (SecretAccessDeniedError, SecretContentRejectedError) as exc:
            # Refused authorship, or content this deployment's moderation will not carry.
            # One bad secret must not cost the importer the rest of the bundle.
            report.skipped.append(f"secret:{ref} ({exc})")
            continue
        report.id_map[ref] = str(row.id)
        report.imported.append(f"secret:{row.gist[:40]}")

        if embedding_provider is not None:
            try:
                from core.ports.embedding import EmbedRequest

                vector = (
                    await embedding_provider.embed(
                        EmbedRequest(model=embedding_provider.model_name, texts=[row.gist])
                    )
                )[0]
                async with tenant_scope(tenant_id) as session:
                    await session.execute(
                        text(
                            "UPDATE secret SET gist_embedding = CAST(:v AS vector) WHERE id = :id"
                        ),
                        {"v": "[" + ",".join(str(x) for x in vector) + "]", "id": row.id},
                    )
            except Exception as exc:  # noqa: BLE001 -- the secret is imported either way
                report.skipped.append(
                    f"gist embedding:{ref} ({exc}); the disclosure gate will not "
                    "consider this secret until it is re-embedded"
                )
        else:
            report.skipped.append(
                f"gist embedding:{ref} (no embedder supplied); the disclosure gate will "
                "not consider this secret until it is re-embedded"
            )

        for holder in record.get("holders") or []:
            principal_id = report.persona_principals.get(str(holder.get("persona_ref")))
            if principal_id is None:
                report.skipped.append(
                    f"holder:{ref} (persona {holder.get('persona_ref')} did not arrive)"
                )
                continue
            await add_holder(
                tenant_id,
                row.id,
                importing_principal_id,
                principal_id,
                str(holder.get("holder_kind") or "author"),
                permission_service=permission_service,
            )
