"""The export/import contract the user asked for, as falsifiable tests:

1. sections are selectable -- a personas-only bundle carries no knowledge/sessions;
2. including connection credentials WITHOUT a password is refused outright
   (plaintext is structurally unavailable for sensitive content), and the encrypted
   bundle never shows the key in the clear;
3. a wrong password fails loudly, the right one round-trips;
4. personas import WITHOUT their connection and bind to the 'missing-connection'
   placeholder (the personas-view warning state); when connections travelled
   (encrypted), the persona binds to the recreated real connection instead.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent, create_persona
from core.agents.models import Agent, Persona
from core.portability.crypto import WrongPasswordError, decrypt_bundle, is_encrypted
from core.portability.export import (
    ExportOptions,
    ExportRequiresEncryptionError,
    export_workspace,
)
from core.portability.import_ import PLACEHOLDER_CONNECTION_NAME, import_bundle
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_API_KEY = "sk-EXPORT-TEST-KEY-THAT-MUST-NOT-LEAK"


class _AllowAllPermissions:
    async def check(self, *args: object, **kwargs: object) -> bool:
        return True


async def _table() -> tuple[uuid.UUID, uuid.UUID, Principal, uuid.UUID]:
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(slug=f"pyr-{uuid.uuid4().hex[:8]}")
    connection = await create_agent(
        tenant_id,
        "cloudy",
        "openai",
        "gpt-test",
        api_key=_API_KEY,
        encryptor=IdentityEncryptor(),
    )
    await create_persona(
        tenant_id,
        workspace_id,
        "traveller",
        "Traveller",
        connection.id,
        persona_type="participant",
        persona_md="A persona that travels between deployments.",
    )
    async with tenant_scope(tenant_id) as session:
        viewer = await session.get(Principal, owner_id)
        session.expunge(viewer)
    return tenant_id, workspace_id, viewer, connection.id


async def test_sections_are_selectable(db_available: None) -> None:
    tenant_id, workspace_id, viewer, _cid = await _table()
    result = await export_workspace(
        viewer,
        tenant_id,
        workspace_id,
        options=ExportOptions(sections=frozenset({"personas"})),
        encryptor=IdentityEncryptor(),
        permission_service=_AllowAllPermissions(),
    )
    paths = list(result.manifest["integrity"].keys())
    assert any(p.startswith("agents/") for p in paths)
    assert not any(p.startswith("knowledge/") for p in paths)
    assert not any(p.startswith("sessions/") for p in paths)
    assert not any(p.startswith("connections/") for p in paths)


async def test_credentials_force_encryption_and_never_leak_plain(db_available: None) -> None:
    tenant_id, workspace_id, viewer, _cid = await _table()
    with pytest.raises(ExportRequiresEncryptionError):
        await export_workspace(
            viewer,
            tenant_id,
            workspace_id,
            options=ExportOptions(sections=frozenset({"personas", "connections"})),
            encryptor=IdentityEncryptor(),
            permission_service=_AllowAllPermissions(),
        )

    result = await export_workspace(
        viewer,
        tenant_id,
        workspace_id,
        options=ExportOptions(
            sections=frozenset({"personas", "connections"}), password="hunter2hunter2"
        ),
        encryptor=IdentityEncryptor(),
        permission_service=_AllowAllPermissions(),
    )
    assert is_encrypted(result.data)
    assert _API_KEY.encode() not in result.data  # the key never appears in the clear

    with pytest.raises(WrongPasswordError):
        decrypt_bundle(result.data, "wrong-password")
    plain = decrypt_bundle(result.data, "hunter2hunter2")
    assert plain.startswith(b"PK")  # the sealed ZIP, intact


async def test_personas_import_without_connection_gets_placeholder(db_available: None) -> None:
    source_tenant, source_ws, viewer, _cid = await _table()
    result = await export_workspace(
        viewer,
        source_tenant,
        source_ws,
        options=ExportOptions(sections=frozenset({"personas"})),
        encryptor=IdentityEncryptor(),
        permission_service=_AllowAllPermissions(),
    )

    dest_tenant, _owner, dest_ws = await seed_dev_tenant(slug=f"pyrd-{uuid.uuid4().hex[:8]}")
    report = await import_bundle(
        result.data,
        dest_tenant,
        dest_ws,
        bundle_ref="test.pyr",
        encryptor=IdentityEncryptor(),
    )
    assert any(entry.startswith("persona:traveller") for entry in report.imported)

    async with tenant_scope(dest_tenant) as session:
        persona = (
            (
                await session.execute(
                    select(Persona).where(
                        Persona.workspace_id == dest_ws, Persona.key == "traveller"
                    )
                )
            )
            .scalars()
            .one()
        )
        connection = await session.get(Agent, persona.agent_id)
    assert connection is not None
    assert connection.name == PLACEHOLDER_CONNECTION_NAME
    assert connection.provider == "none"  # the UI's warning trigger


async def test_personas_import_with_travelled_connections_binds_real(db_available: None) -> None:
    source_tenant, source_ws, viewer, _cid = await _table()
    result = await export_workspace(
        viewer,
        source_tenant,
        source_ws,
        options=ExportOptions(
            sections=frozenset({"personas", "connections"}), password="pw-pw-pw-pw"
        ),
        encryptor=IdentityEncryptor(),
        permission_service=_AllowAllPermissions(),
    )

    dest_tenant, _owner, dest_ws = await seed_dev_tenant(slug=f"pyrc-{uuid.uuid4().hex[:8]}")
    report = await import_bundle(
        result.data,
        dest_tenant,
        dest_ws,
        bundle_ref="test.pyr",
        password="pw-pw-pw-pw",
        encryptor=IdentityEncryptor(),
    )
    assert any(entry == "connection:cloudy" for entry in report.imported)

    async with tenant_scope(dest_tenant) as session:
        persona = (
            (
                await session.execute(
                    select(Persona).where(
                        Persona.workspace_id == dest_ws, Persona.key == "traveller"
                    )
                )
            )
            .scalars()
            .one()
        )
        connection = await session.get(Agent, persona.agent_id)
    assert connection is not None and connection.name == "cloudy"
    assert connection.provider == "openai"
    # the credential was re-sealed locally, not dropped
    assert connection.credential_ref is not None
