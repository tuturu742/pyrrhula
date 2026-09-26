"""Acceptance criteria for `.pyr` export: the participant slice matches the
resolver's answer and lists every omission, the service delegates all visibility to that
resolver (a *static* assertion over its source), integrity hashes catch tampering, and a
turn replays from bundle contents alone.

Under ``tests/isolation/`` for the ``two_tenants`` fixture. No new table -- export reads.
"""

from __future__ import annotations

import ast
import hashlib
import io
import pathlib
import uuid
import zipfile

import pytest
from sqlalchemy import select, text

from adapters.encryptor.identity import IdentityEncryptor
from adapters.permission.role_permission import RolePermissionService
from core.agents.seed import seed_dev_agent
from core.assembler.context_assembler import assemble
from core.assembler.manifest import write_context_manifest
from core.assembler.visibility import seed_default_scopes
from core.entities.repo import save_schema
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import create_entity
from core.knowledge.authoring import (
    EntryFields,
    attach_source_to_workspace,
    create_source,
    publish_version,
    upsert_draft_entry,
)
from core.portability.bundle import (
    BundleIntegrityError,
    BundleReader,
    open_bundle,
    verify_bundle,
)
from core.portability.export import export_workspace
from core.portability.replay import BundleReplayError, replays_from_bundle
from core.process.dsl.schema import ActorSpec, BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.skeleton import create_session
from core.tenancy.models import Principal, Workspace, WorkspaceMembership
from core.tenancy.scope import tenant_scope

_PERMISSIONS = RolePermissionService()
_ENCRYPTOR = IdentityEncryptor()
_ROOT = pathlib.Path(__file__).resolve().parents[2]
_EXPORT_SOURCE_PATH = _ROOT / "packages" / "core" / "portability" / "export.py"


async def _workspace_of(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _member(tenant_id: uuid.UUID, workspace_id: uuid.UUID, role: str) -> Principal:
    async with tenant_scope(tenant_id) as session:
        principal = Principal(tenant_id=tenant_id, kind="human", display_name=role)
        session.add(principal)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal.id,
                role=role,
            )
        )
        await session.flush()
        session.expunge(principal)
        return principal


async def _seed_two_scoped_entries(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> tuple[uuid.UUID, str, str]:
    """A source with one public and one facilitator-only entry, published as v1 and
    attached to the workspace. Returns ``(source_id, public_key, hidden_key)``."""
    source = await create_source(
        tenant_id, key=f"src-{uuid.uuid4().hex[:8]}", name="Field Guide", class_="lore"
    )
    public_key = "open-gate"
    hidden_key = "sealed-vault"
    await upsert_draft_entry(
        tenant_id,
        source.id,
        public_key,
        EntryFields(
            title="The Open Gate",
            body_md="The gate stands open to anyone who asks.",
            class_="lore",
            scope_key="workspace_public",
        ),
    )
    await upsert_draft_entry(
        tenant_id,
        source.id,
        hidden_key,
        EntryFields(
            title="The Sealed Vault",
            body_md="Behind the third pillar, a vault nobody speaks of.",
            class_="lore",
            scope_key="facilitator_only",
        ),
    )
    version = await publish_version(tenant_id, source.id)
    await attach_source_to_workspace(
        tenant_id, workspace_id, source.id, "workspace_public", version_pin=version.id
    )
    return source.id, public_key, hidden_key


async def _seed_chunks_for_published_entries(tenant_id: uuid.UUID) -> None:
    """Published entries carry no chunks -- chunking is the ingestion pipeline's job
     -- and both the assembler and the export's chunk section work on *chunks*. One
    chunk per published entry, seeded the same way ``test_context_assembler`` does, plus
    ``constant = true`` so activation includes them without a real embedding model in the
    loop. Each chunk inherits its entry's ``scope_key``, which is what makes the chunk
    section's scope filter testable rather than vacuous."""
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text("UPDATE knowledge_entry SET constant = true WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO knowledge_chunk "
                "(tenant_id, entry_id, version_id, ordinal, text, token_count, class, "
                " scope_key, embedding, content_hash) "
                "SELECT e.tenant_id, e.id, e.version_id, 0, e.body_md, 8, e.class, "
                "       e.scope_key, NULL, md5(e.body_md) "
                "FROM knowledge_entry e "
                "WHERE e.tenant_id = :t AND e.version_id IS NOT NULL"
            ),
            {"t": tenant_id},
        )


async def test_participant_export_matches_visibility_and_lists_redactions(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    facilitator = await _member(tenant_id, workspace_id, "facilitator")
    participant = await _member(tenant_id, workspace_id, "participant")
    _source_id, public_key, hidden_key = await _seed_two_scoped_entries(tenant_id, workspace_id)
    await _seed_chunks_for_published_entries(tenant_id)

    definition = EntitySchemaDefinition(fields=[FieldDef(key="hp", type="integer")])
    schema_row = await save_schema(tenant_id, workspace_id, "npc", 1, definition)
    public_entity = await create_entity(
        tenant_id,
        workspace_id,
        schema_row.id,
        definition,
        key=f"pub-{uuid.uuid4().hex[:8]}",
        name="Innkeeper",
        scope_key="workspace_public",
        data={"hp": 8},
    )
    hidden_entity = await create_entity(
        tenant_id,
        workspace_id,
        schema_row.id,
        definition,
        key=f"hid-{uuid.uuid4().hex[:8]}",
        name="The Culprit",
        scope_key="facilitator_only",
        data={"hp": 30},
    )

    full = await export_workspace(
        facilitator,
        tenant_id,
        workspace_id,
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
    )
    limited = await export_workspace(
        participant,
        tenant_id,
        workspace_id,
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
    )

    facilitator_reader = open_bundle(full.data)
    participant_reader = open_bundle(limited.data)

    def entry_bodies(reader: BundleReader) -> str:
        return "\n".join(
            reader.read_text(p) for p in reader.paths_under("knowledge/") if p.endswith(".md")
        )

    # The facilitator's slice has both; the participant's has exactly the public one.
    assert public_key in "".join(facilitator_reader.paths_under("knowledge/"))
    assert hidden_key in "".join(facilitator_reader.paths_under("knowledge/"))
    assert public_key in "".join(participant_reader.paths_under("knowledge/"))
    assert hidden_key not in "".join(participant_reader.paths_under("knowledge/"))
    assert "vault nobody speaks of" not in entry_bodies(participant_reader)

    assert f"entities/entity_{public_entity.id}.json" in participant_reader.files
    assert f"entities/entity_{hidden_entity.id}.json" not in participant_reader.files
    assert f"entities/entity_{hidden_entity.id}.json" in facilitator_reader.files

    # Every excluded object appears as a stub -- verified by diffing against the full
    # export, which is the criterion's own method.
    only_in_full = set(facilitator_reader.files) - set(participant_reader.files)
    assert only_in_full, "the two exports were identical; the fixture proves nothing"

    redacted_ids = {r["id"] for r in participant_reader.redactions}
    assert str(hidden_entity.id) in redacted_ids
    assert {"entity", "knowledge_entry", "knowledge_chunk"} <= {
        r["type"] for r in participant_reader.redactions
    }
    for redaction in participant_reader.redactions:
        assert redaction["reason"], "a redaction stub with no reason is a silent omission"


def test_export_service_delegates_all_visibility_to_the_resolver() -> None:
    """A *static* assertion over `export.py`'s own source (: "ExportService must not
    have its own visibility logic"). Runtime tests can only ever show that today's export
    happens to agree with the resolver; this shows there is no second implementation to
    disagree with it tomorrow."""
    source = _EXPORT_SOURCE_PATH.read_text()
    tree = ast.parse(source, filename=str(_EXPORT_SOURCE_PATH))

    resolver_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "scopes_for"
    ]
    assert len(resolver_calls) == 1, (
        f"expected exactly one scopes_for() call in export.py, found {len(resolver_calls)} -- "
        "visibility is resolved once, at the top, and filtered against thereafter"
    )

    # Nothing that would let this module decide visibility for itself. Asserted on
    # imported *names*, not modules: `ContextManifestRow` legitimately lives beside
    # `ScopeRow` in core.assembler.models, and banning the module would ban an unrelated
    # read while banning the name bans exactly the thing that matters.
    forbidden_names = {"ScopeRow", "WorkspaceMembership", "_grants"}
    forbidden_modules = {"core.knowledge.repo", "core.secrets.repo"}
    imported_names: set[str] = set()
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            imported_modules.update(a.name for a in node.names)
    assert not (imported_modules & forbidden_modules), (
        f"export.py imports {sorted(imported_modules & forbidden_modules)} -- INV-1's own "
        "lint says the same thing, and this is the export-specific half of it"
    )
    assert not (imported_names & forbidden_names), (
        f"export.py imports {sorted(imported_names & forbidden_names)} -- these are how a "
        "second visibility implementation gets built by accident"
    )
    # `Persona` itself is imported legitimately -- agents are workspace *content* an export
    # carries. What must never happen is export.py *comparing* on persona_type, which is how
    # role resolution would get reimplemented here.
    compared = [
        node
        for cmp_node in ast.walk(tree)
        if isinstance(cmp_node, ast.Compare)
        for node in ast.walk(cmp_node)
        if isinstance(node, ast.Attribute) and node.attr == "persona_type"
    ]
    assert not compared, (
        "export.py compares on persona_type -- that is role resolution, and it belongs to "
        "the resolver, which already answered"
    )

    # No literal scope key: a hardcoded compartment name is a visibility decision made
    # here rather than resolved.
    for literal in ("workspace_public", "facilitator_only", "agent_private"):
        assert literal not in source, (
            f"export.py hardcodes the scope key {literal!r} -- scope membership is the "
            "resolver's answer, never a constant in this file"
        )


async def test_bundle_integrity_hashes_detect_tampering(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    facilitator = await _member(tenant_id, workspace_id, "facilitator")
    _source_id, public_key, _hidden_key = await _seed_two_scoped_entries(tenant_id, workspace_id)

    result = await export_workspace(
        facilitator,
        tenant_id,
        workspace_id,
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
    )

    clean = open_bundle(result.data)
    assert verify_bundle(clean) == []
    # Every file is covered -- an integrity map that silently skipped one would pass the
    # check above while protecting nothing.
    assert set(clean.manifest["integrity"]) == set(clean.files)
    for path, digest in clean.manifest["integrity"].items():
        assert hashlib.sha256(clean.files[path]).hexdigest() == digest

    target = next(p for p in clean.files if p.endswith(f"{public_key}.md"))
    tampered = _rewrite_zip_entry(result.data, target, b"The gate is barred. Turn back.")

    reopened = open_bundle(tampered)
    assert verify_bundle(reopened) == [target]

    # Adding a file nobody listed is caught by the same check, from the other direction.
    smuggled = _rewrite_zip_entry(result.data, "assets/extra.txt", b"not in the manifest")
    assert verify_bundle(open_bundle(smuggled)) == ["assets/extra.txt"]

    with pytest.raises(BundleIntegrityError):
        open_bundle(_strip_zip_entry(result.data, "manifest.json"))


async def test_replay_from_bundle_alone_is_byte_identical(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """INV-10 across the export boundary. The turn is deliberately knowledge-only (no
    history, no entity state, no secrets), so its whole ``rendered_hash`` is the knowledge
    block -- see ``core.portability.replay``'s docstring for why a turn carrying a
    concealed secret is *not* replayable from a bundle, and why that is correct."""
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    facilitator = await _member(tenant_id, workspace_id, "facilitator")
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    await _seed_two_scoped_entries(tenant_id, workspace_id)

    await _seed_chunks_for_published_entries(tenant_id)

    phase = PhaseSpec(
        label_key="turn",
        actors=[ActorSpec(persona_type="supervisor", mode="generate")],
        visibility=VisibilitySpec(
            knowledge_classes=["lore"],
            scopes=["workspace_public", "facilitator_only"],
            entity_fields="all",
            secrets="none",
        ),
        budget=BudgetSpec(ratio={"lore": 1.0}, max_tokens=4000),
    )
    manifest = await assemble(
        facilitator,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=sess.id,
        query_text="gate",
        query_embedding=[0.0] * 1024,
        history_max_tokens=0,
    )
    assert manifest.entries, "the fixture produced an empty context; the replay would be vacuous"
    manifest_row = await write_context_manifest(
        tenant_id, sess.id, 0, facilitator.id, phase.label_key, manifest
    )

    result = await export_workspace(
        facilitator,
        tenant_id,
        workspace_id,
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
    )
    reader = open_bundle(result.data)
    assert verify_bundle(reader) == []

    records = reader.read_jsonl(f"sessions/session_{sess.id}/manifests.jsonl")
    record = next(r for r in records if r["event_seq"] == 0)
    assert record["rendered_hash"] == manifest_row.rendered_hash

    assert replays_from_bundle(reader, record), (
        "the turn did not replay byte-identically from bundle contents alone"
    )

    # And the proof has teeth: strip a chunk the manifest cites and the replay refuses
    # rather than quietly rendering something shorter.
    cited_chunk_id = str(record["entries"][0]["chunk_id"])
    chunk_path = next(
        p for p in reader.paths_under("knowledge/") if p.endswith(f"/chunks/{cited_chunk_id}.json")
    )
    stripped = open_bundle(_strip_zip_entry(result.data, chunk_path))
    with pytest.raises(BundleReplayError):
        replays_from_bundle(stripped, record)


def _rewrite_zip_entry(data: bytes, path: str, replacement: bytes) -> bytes:
    """Rebuilds the archive with one file's bytes replaced (or added). Deliberately leaves
    `manifest.json` untouched -- that is what a tamperer would do, and what the integrity
    map exists to catch."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buffer, "w") as dst:
        names = src.namelist()
        for name in names:
            dst.writestr(name, replacement if name == path else src.read(name))
        if path not in names:
            dst.writestr(path, replacement)
    return buffer.getvalue()


def _strip_zip_entry(data: bytes, path: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buffer, "w") as dst:
        for name in src.namelist():
            if name != path:
                dst.writestr(name, src.read(name))
    return buffer.getvalue()
