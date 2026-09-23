"""What a `.pyr` exports, it must import.

Export has always written `process/`, `vocabulary/` and `secrets/`; import walked past
all three. A bundle that arrives without its flow, its labels and its secrets is not a
portable workspace -- it is a pile of knowledge entries with no way to run them, and the
failure is silent, which is the worst property a portability format can have.

These tests fix the shape of the fix: the round trip carries the parts that make a
workspace runnable, a sanitised bundle still carries none of the sensitive ones, and a
guard fails the next time a section is added to the writer and forgotten in the reader.
"""

from __future__ import annotations

import json
import pathlib
import re
import uuid

import pytest
from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from adapters.moderation.allow_all import AllowAllModerationProvider
from adapters.permission.role_permission import RolePermissionService
from core.agents.authoring import create_agent, create_persona
from core.agents.models import Persona
from core.assembler.visibility import seed_default_scopes
from core.portability.bundle import open_bundle
from core.portability.export import ExportOptions, export_workspace
from core.portability.import_ import import_bundle
from core.process.authoring import create_definition, list_definitions
from core.process.dsl.fixtures import MINIMAL_MVP_FLOW
from core.secrets.authoring import add_holder, create_secret
from core.secrets.models import SecretHolderRow, SecretRow
from core.tenancy.models import Principal, Workspace, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.vocabulary.service import get_overlay_by_key, set_workspace_overlay, upsert_overlay

_PERMISSIONS = RolePermissionService()
_ENCRYPTOR = IdentityEncryptor()
_MODERATION = AllowAllModerationProvider()

_SECRET_CONTENT = "You took page six from the desk and turned the bath on so nobody could time it."


async def _workspace_of(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _member(tenant_id: uuid.UUID, workspace_id: uuid.UUID, role: str) -> Principal:
    async with tenant_scope(tenant_id) as session:
        principal = Principal(tenant_id=tenant_id, kind="human", display_name=f"{role}-person")
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


async def _case_workspace(tenant_id: uuid.UUID) -> tuple[uuid.UUID, Principal, Persona]:
    """A workspace shaped like the thing this feature exists for: a flow, a vocabulary
    overlay, a persona, and a secret that persona holds -- and that the human referee
    holds too, which is what lets an unencrypted participant export carry it."""
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    referee = await _member(tenant_id, workspace_id, "facilitator")

    await create_definition(
        tenant_id, "case", "The Case", MINIMAL_MVP_FLOW, workspace_id=workspace_id
    )

    overlay = await upsert_overlay(
        tenant_id,
        f"case-labels-{uuid.uuid4().hex[:6]}",
        "Case labels",
        {"entity.workspace": "Table"},
    )
    await set_workspace_overlay(tenant_id, workspace_id, overlay.id)

    connection = await create_agent(
        tenant_id, f"conn-{uuid.uuid4().hex[:6]}", "none", "unconfigured", encryptor=_ENCRYPTOR
    )
    persona = await create_persona(
        tenant_id,
        workspace_id,
        "elin",
        "Elin Wallmark",
        connection.id,
        persona_md="You are Elin Wallmark, glass artist.",
    )

    secret = await create_secret(
        tenant_id,
        workspace_id,
        referee.id,
        subject_kind="agent",
        subject_id=persona.id,
        content=_SECRET_CONTENT,
        gist="elin was in the study at seven",
        scope_key="workspace_public",
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
        moderation_provider=_MODERATION,
        hint_text="She changed her dress.",
        behavioral_directive="Do not get caught.",
    )
    # The character knows it; so does the referee who wrote the case. Both are true, and
    # only the second is what makes a participant-mode export carry the content.
    await add_holder(
        tenant_id,
        secret.id,
        referee.id,
        persona.principal_id,
        "author",
        permission_service=_PERMISSIONS,
    )
    await add_holder(
        tenant_id, secret.id, referee.id, referee.id, "author", permission_service=_PERMISSIONS
    )
    return workspace_id, referee, persona


async def _import_into(
    data: bytes, tenant_id: uuid.UUID, workspace_id: uuid.UUID, principal_id: uuid.UUID
):
    return await import_bundle(
        data,
        tenant_id,
        workspace_id,
        bundle_ref="parity",
        encryptor=_ENCRYPTOR,
        importing_principal_id=principal_id,
        permission_service=_PERMISSIONS,
        moderation_provider=_MODERATION,
    )


@pytest.mark.asyncio
async def test_a_workspace_round_trips_with_its_flow_labels_and_secrets(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    workspace_a, referee, persona_a = await _case_workspace(tenant_a)
    workspace_b = await _workspace_of(tenant_b)
    await seed_default_scopes(tenant_b, workspace_b)
    importer = await _member(tenant_b, workspace_b, "facilitator")

    result = await export_workspace(
        referee, tenant_a, workspace_a, encryptor=_ENCRYPTOR, permission_service=_PERMISSIONS
    )
    report = await _import_into(result.data, tenant_b, workspace_b, importer.id)

    definitions = {row.key for row in await list_definitions(tenant_b, workspace_id=workspace_b)}
    assert "case" in definitions, "the flow did not arrive; the workspace cannot be run"

    async with tenant_scope(tenant_b) as session:
        workspace = await session.get(Workspace, workspace_b)
        assert workspace is not None and workspace.vocabulary_overlay_id is not None

        secrets = list(
            (
                await session.execute(
                    select(SecretRow).where(SecretRow.workspace_id == workspace_b)
                )
            ).scalars()
        )
        assert len(secrets) == 1, f"expected the case's one secret, got {len(secrets)}"
        secret = secrets[0]
        assert _ENCRYPTOR.decrypt(secret.content_ciphertext) == _SECRET_CONTENT
        assert secret.behavioral_directive == "Do not get caught."

        persona_b = (
            await session.execute(
                select(Persona).where(Persona.workspace_id == workspace_b, Persona.key == "elin")
            )
        ).scalar_one()
        # The secret must point at the imported persona, not at a stale id from the
        # exporting tenant -- a subject that dangles is a secret about nobody.
        assert secret.subject_id == persona_b.id

        holders = list(
            (
                await session.execute(
                    select(SecretHolderRow).where(SecretHolderRow.secret_id == secret.id)
                )
            ).scalars()
        )

    holder_principals = {h.holder_principal_id for h in holders}
    assert persona_b.principal_id in holder_principals, (
        "the character does not hold her own secret: the exclusion path has nothing to "
        "scope and the gate has nothing to govern"
    )
    assert importer.id not in holder_principals, (
        "the importing human was silently made a holder -- who-knows-what must not be "
        "widened by moving a bundle between deployments"
    )
    assert any(item.startswith("process:") for item in report.imported)
    assert any(item.startswith("secret:") for item in report.imported)


@pytest.mark.asyncio
async def test_a_sanitised_bundle_carries_no_secret_and_says_so(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Sanitised export writes no content, so there is nothing to import -- and no holder
    set either: who knows a secret is itself a who-knows-what fact."""
    tenant_a, tenant_b = two_tenants
    workspace_a, referee, _ = await _case_workspace(tenant_a)
    workspace_b = await _workspace_of(tenant_b)
    await seed_default_scopes(tenant_b, workspace_b)
    importer = await _member(tenant_b, workspace_b, "facilitator")

    result = await export_workspace(
        referee,
        tenant_a,
        workspace_a,
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
        options=ExportOptions(mode="sanitised"),
    )
    reader = open_bundle(result.data)
    secret_files = [p for p in reader.files if p.startswith("secrets/")]
    assert secret_files, "the secret's existence should still be visible as metadata"
    for path in secret_files:
        payload = json.loads(reader.files[path].decode())
        assert "content" not in payload
        assert "holders" not in payload

    report = await _import_into(result.data, tenant_b, workspace_b, importer.id)
    async with tenant_scope(tenant_b) as session:
        landed = list(
            (
                await session.execute(
                    select(SecretRow).where(SecretRow.workspace_id == workspace_b)
                )
            ).scalars()
        )
    assert landed == [], "a contentless secret must not be imported as a hollow row"
    assert any("no content in this bundle" in item for item in report.skipped)


@pytest.mark.asyncio
async def test_a_colliding_flow_key_forks_instead_of_replacing(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    workspace_a, referee, _ = await _case_workspace(tenant_a)
    workspace_b = await _workspace_of(tenant_b)
    await seed_default_scopes(tenant_b, workspace_b)
    importer = await _member(tenant_b, workspace_b, "facilitator")
    resident = await create_definition(
        tenant_b, "case", "The resident case", MINIMAL_MVP_FLOW, workspace_id=workspace_b
    )

    result = await export_workspace(
        referee, tenant_a, workspace_a, encryptor=_ENCRYPTOR, permission_service=_PERMISSIONS
    )
    report = await _import_into(result.data, tenant_b, workspace_b, importer.id)

    assert ("case", "case-imported") in report.forked_keys
    rows = {row.key: row for row in await list_definitions(tenant_b, workspace_id=workspace_b)}
    assert rows["case"].id == resident.id, "a session pinned to the resident flow lost it"
    assert "case-imported" in rows


@pytest.mark.asyncio
async def test_vocabulary_binds_to_the_overlay_already_here_rather_than_cloning_it(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Export writes whatever the workspace resolved to, which is usually one of the
    deployment's own system overlays. Cloning it per import would multiply `rpg_v1`
    without bound."""
    tenant_a, tenant_b = two_tenants
    workspace_a = await _workspace_of(tenant_a)
    await seed_default_scopes(tenant_a, workspace_a)
    referee = await _member(tenant_a, workspace_a, "facilitator")
    shared_key = f"shared-labels-{uuid.uuid4().hex[:6]}"
    overlay_a = await upsert_overlay(tenant_a, shared_key, "Shared", {"entity.workspace": "Table"})
    await set_workspace_overlay(tenant_a, workspace_a, overlay_a.id)

    workspace_b = await _workspace_of(tenant_b)
    await seed_default_scopes(tenant_b, workspace_b)
    importer = await _member(tenant_b, workspace_b, "facilitator")
    resident = await upsert_overlay(tenant_b, shared_key, "Resident", {"entity.workspace": "Board"})

    result = await export_workspace(
        referee, tenant_a, workspace_a, encryptor=_ENCRYPTOR, permission_service=_PERMISSIONS
    )
    await _import_into(result.data, tenant_b, workspace_b, importer.id)

    async with tenant_scope(tenant_b) as session:
        workspace = await session.get(Workspace, workspace_b)
        assert workspace is not None
        assert workspace.vocabulary_overlay_id == resident.id
    bound = await get_overlay_by_key(tenant_b, shared_key)
    assert bound is not None and bound.labels["entity.workspace"] == "Board", (
        "the incoming overlay overwrote the resident one -- import is additive"
    )


# ── the guard ────────────────────────────────────────────────────────────────────────

_SRC = pathlib.Path(__file__).resolve().parents[2] / "packages" / "core" / "portability"
# Written by export, deliberately not read by import, with the reason. Anything else new
# has to be handled or added here on purpose.
_NOT_IMPORTED = {
    # The importer supplies its own target workspace; the exporter's name and settings
    # are provenance, not something to graft onto someone else's workspace.
    "workspace.json",
}


def _sections_written_by_export() -> set[str]:
    """Every top-level bundle path export can write.

    Two of them are written through a computed ``prefix`` variable rather than a literal
    (``knowledge/ks_<id>``, ``sessions/session_<id>``), so those assignments are read too
    -- a guard that quietly skipped the two largest sections would be worse than none."""
    source = (_SRC / "export.py").read_text()
    literals = set(re.findall(r'writer\.add_\w+\(\s*f?"([^"{}]+)', source))
    literals |= set(re.findall(r'prefix = f"([a-z_]+)/', source))
    sections = {path.split("/")[0] for path in literals}
    assert {"knowledge", "sessions"} <= sections, (
        "the prefix assignments in export.py moved: this guard is no longer reading "
        "the sections it claims to"
    )
    return sections


# Named files export writes *inside* a section, with the reader token that proves import
# looks at each. A top-level-only guard missed `attachments.json` for a year: the entries
# imported, attached to nothing, and every agent silently ran without its handbook.
_NAMED_FILES = {
    "attachments.json": "attachments.json",
    "meta.json": "/meta.json",
    "chunks/": "/chunks/",
    "events.jsonl": "events.jsonl",
    "checkpoints.jsonl": "checkpoints.jsonl",
    "resolutions.jsonl": "resolutions.jsonl",
    "connections/connections.json": "connections/connections.json",
}


def test_every_named_file_export_writes_is_read_by_import() -> None:
    """The finer-grained half of the guard above.

    A section can be 'handled' while a file inside it is dropped, and that failure is
    invisible: the rows land, nothing errors, and the workspace merely behaves as though
    the content were not there."""
    export_source = (_SRC / "export.py").read_text()
    reader_source = (_SRC / "import_.py").read_text()
    missing = sorted(
        name
        for name, token in _NAMED_FILES.items()
        if name in export_source and token not in reader_source
    )
    assert not missing, (
        f"export writes {missing} and import never reads it. The rows arrive and nothing "
        "fails -- the workspace just behaves as if the content were absent."
    )


def test_every_section_export_writes_is_read_by_import() -> None:
    """The regression this whole file exists for. `process/`, `vocabulary/` and
    `secrets/` were written for a year and never read, and nothing failed -- the bundle
    was simply poorer on the other side. A section added to the writer and forgotten in
    the reader fails here instead of in someone's imported workspace."""
    reader_source = (_SRC / "import_.py").read_text()
    missing = sorted(
        section
        for section in _sections_written_by_export() - _NOT_IMPORTED
        if f'"{section}/' not in reader_source and f'startswith("{section}' not in reader_source
    )
    assert not missing, (
        f"export writes {missing} and import never reads it: a bundle would arrive "
        "missing that section, silently. Handle it in import_.py, or add it to "
        "_NOT_IMPORTED with the reason."
    )


@pytest.mark.asyncio
async def test_an_entity_arrives_in_the_state_it_left_in(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A `.pyr` carried the sheet and dropped the situation. Export wrote ``data`` and not
    ``fsm_states``, so a bloodied character, a work item in review, a quest half-finished
    all landed in the importing deployment as if nothing had happened to them -- silently,
    which is the failure mode this file exists to prevent.
    """
    from core.entities.mutation import transition
    from core.entities.repo import save_schema
    from core.entities.schema import EntitySchemaDefinition, FieldDef
    from core.entities.storage import EntityRow, create_entity

    tenant_a, tenant_b = two_tenants
    workspace_a, referee, _persona_a = await _case_workspace(tenant_a)
    workspace_b = await _workspace_of(tenant_b)
    await seed_default_scopes(tenant_b, workspace_b)
    importer = await _member(tenant_b, workspace_b, "facilitator")

    definition = EntitySchemaDefinition(
        fields=[FieldDef(key="name", type="string")],
        state_machines=[
            {
                "key": "health",
                "states": [
                    {"key": "healthy", "label_key": "entity.health.healthy"},
                    {"key": "bloodied", "label_key": "entity.health.bloodied"},
                ],
                "initial": "healthy",
                "transitions": [{"from": "healthy", "to": "bloodied", "trigger": "wound"}],
            }
        ],
    )
    schema_row = await save_schema(tenant_a, workspace_a, "traveller", 1, definition)
    entity = await create_entity(
        tenant_a,
        workspace_a,
        schema_row.id,
        definition,
        key=f"traveller-{uuid.uuid4().hex[:8]}",
        name="Bram",
        scope_key="workspace_public",
        data={"name": "Bram"},
    )
    await transition(
        referee.id,
        tenant_a,
        workspace_a,
        entity.id,
        "health",
        "wound",
        f"wound-{uuid.uuid4()}",
        permission_service=_PERMISSIONS,
    )

    result = await export_workspace(
        referee, tenant_a, workspace_a, encryptor=_ENCRYPTOR, permission_service=_PERMISSIONS
    )
    await _import_into(result.data, tenant_b, workspace_b, importer.id)

    async with tenant_scope(tenant_b) as session:
        rows = list(
            (
                await session.execute(
                    select(EntityRow).where(EntityRow.workspace_id == workspace_b)
                )
            ).scalars()
        )
    assert len(rows) == 1, f"expected the one traveller, got {len(rows)}"
    assert rows[0].fsm_states == {"health": "bloodied"}, (
        "the character arrived healthy: machine state did not survive the bundle"
    )


@pytest.mark.asyncio
async def test_a_schema_keeps_its_key_on_the_way_into_an_empty_workspace(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Entity schemas forked unconditionally, unlike flows and knowledge sources, which
    fork only when a resident key is actually taken. The cost fell on the one thing a
    schema carries that nothing else does -- its state machines. They arrived under
    ``character-imported``, a key no persona, tool or flow in the importing workspace
    refers to, so an FSM that round-tripped perfectly was attached to nothing.
    """
    from core.entities.repo import list_latest_schemas, save_schema
    from core.entities.schema import EntitySchemaDefinition, FieldDef

    tenant_a, tenant_b = two_tenants
    workspace_a, referee, _persona_a = await _case_workspace(tenant_a)
    workspace_b = await _workspace_of(tenant_b)
    await seed_default_scopes(tenant_b, workspace_b)
    importer = await _member(tenant_b, workspace_b, "facilitator")

    definition = EntitySchemaDefinition(
        fields=[FieldDef(key="name", type="string")],
        state_machines=[
            {
                "key": "health",
                "states": [
                    {"key": "healthy", "label_key": "entity.health.healthy"},
                    {"key": "bloodied", "label_key": "entity.health.bloodied"},
                ],
                "initial": "healthy",
                "transitions": [{"from": "healthy", "to": "bloodied", "trigger": "wound"}],
            }
        ],
    )
    await save_schema(tenant_a, workspace_a, "traveller", 1, definition)

    result = await export_workspace(
        referee, tenant_a, workspace_a, encryptor=_ENCRYPTOR, permission_service=_PERMISSIONS
    )
    await _import_into(result.data, tenant_b, workspace_b, importer.id)

    keys = {row.key for row in await list_latest_schemas(tenant_b, workspace_b)}
    assert "traveller" in keys, f"the schema was renamed on arrival: {sorted(keys)}"

    # Importing the same bundle again must not graft a second definition onto the
    # resident schema's version history -- that collision is what forking is for.
    await _import_into(result.data, tenant_b, workspace_b, importer.id)
    keys = {row.key for row in await list_latest_schemas(tenant_b, workspace_b)}
    assert "traveller-imported" in keys, f"the second copy did not fork: {sorted(keys)}"
