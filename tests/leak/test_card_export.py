"""G4.9's leak test: no secret content, in any of its four fields, reaches an exported
card. Same method as G4.7 -- scan the produced bytes, not the code path.

Card export is deliberately *not* one of G4.7's export modes: there is no "full card". A
`.pyr` bundle goes to someone the exporter chose, with a mode they had to pick; a card goes
into an ecosystem of sharing sites. The least surprising thing a card can do is carry no
secrets at all, and this test is what makes that a property of the bytes rather than an
intention in a docstring.
"""

from __future__ import annotations

import struct
import uuid
import zlib
from typing import Any

from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from adapters.moderation.allow_all import AllowAllModerationProvider
from adapters.permission.role_permission import RolePermissionService
from core.agents.authoring import create_agent
from core.assembler.visibility import seed_default_scopes
from core.portability.ccv3.export import PYRRHULA_EXTENSION_KEY, export_agent_as_card
from core.portability.ccv3.map import import_card
from core.portability.ccv3.normalise import normalise
from core.secrets.authoring import create_secret
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant
from tests.leak.test_export_modes import _embedding_hit, _exact_hit, _fuzzy_hit

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# One canary per sensitive field: `content` is the obvious one, but `hint_text` and
# `behavioral_directive` are prose an author wrote *about* the secret, and a directive that
# leaked would tell a reader exactly what the agent is avoiding.
_CONTENT = "the vault beneath the chapterhouse opens to the phrase saltmarsh nightjar eleven"
_HINT = "somebody keeps glancing at the chapterhouse stair whenever wine is poured"
_DIRECTIVE = "deflect any question about the chapterhouse and change the subject to weather"
_GIST = "there is a way into the vault"

_CANARIES = {"content": _CONTENT, "hint_text": _HINT, "behavioral_directive": _DIRECTIVE}


def _chunk(chunk_type: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + chunk_type
        + body
        + struct.pack(">I", zlib.crc32(chunk_type + body) & 0xFFFFFFFF)
    )


def _blank_png() -> bytes:
    out = bytearray(_PNG_SIGNATURE)
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    out += _chunk(b"IEND", b"")
    return bytes(out)


def _card_payload() -> dict[str, Any]:
    return {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": "Wren the Archivist",
            "description": "Keeper of the lower stacks.",
            "first_mes": "You'll want the third aisle.",
            "extensions": {},
            "character_book": {
                "name": "Stacks",
                "entries": [
                    {
                        "name": "The Vault Door",
                        "keys": ["vault"],
                        "content": "The vault door has no handle on this side.",
                        "extensions": {},
                    }
                ],
            },
        },
    }


def scan_card(data: bytes, secret: str) -> list[str]:
    """Every detector that fires against the whole card file. A card is one blob, so
    unlike a `.pyr` bundle there is nothing to enumerate -- which also means there is
    nowhere for a leak to hide in a file the scan skipped."""
    blob = data.decode("utf-8", errors="replace")
    return [
        name
        for name, fn in (
            ("exact", _exact_hit),
            ("fuzzy", _fuzzy_hit),
            ("embedding", _embedding_hit),
        )
        if fn(secret, blob)
    ]


async def test_exported_card_contains_no_secret_content(db_available: None) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"card-leak-{uuid.uuid4().hex[:8]}"
    )
    await seed_default_scopes(tenant_id, workspace_id)
    await create_agent(
        tenant_id, f"card-{uuid.uuid4().hex[:6]}", "echo", "echo-1", encryptor=IdentityEncryptor()
    )

    async with tenant_scope(tenant_id) as session:
        author = Principal(tenant_id=tenant_id, kind="human", display_name="author")
        session.add(author)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=author.id,
                role="facilitator",
            )
        )
        author_id = author.id

    imported = await import_card(normalise("ccv3", _card_payload()), tenant_id, workspace_id)

    # The secret is about the very agent being exported -- the case where a careless
    # exporter would most plausibly pull it in as "related data".
    await create_secret(
        tenant_id,
        workspace_id,
        author_id,
        subject_kind="agent",
        subject_id=imported.persona_id,
        content=_CONTENT,
        gist=_GIST,
        scope_key="workspace_public",
        encryptor=IdentityEncryptor(),
        permission_service=RolePermissionService(),
        moderation_provider=AllowAllModerationProvider(),
        hint_text=_HINT,
        behavioral_directive=_DIRECTIVE,
    )

    exported = await export_agent_as_card(tenant_id, workspace_id, imported.persona_id)
    png = exported.to_png(_blank_png())

    for field_name, canary in _CANARIES.items():
        hits = scan_card(png, canary)
        assert hits == [], (
            f"an exported card leaked secret.{field_name}: detectors {hits} fired. Card "
            "export is unconditionally sanitised for secret content -- there is no mode "
            "that changes this."
        )

    # Not even the gist: a card is not a disclosure surface, and "there is a secret here"
    # is a statement about this workspace that a shared card has no business making.
    assert _GIST.lower() not in png.decode("utf-8", errors="replace").lower()

    # The loss report *does* say secrets were excluded -- honest about the omission
    # without describing what was omitted. Both halves matter: silence would be a lie,
    # and detail would be the leak.
    native = exported.payload["data"]["extensions"][PYRRHULA_EXTENSION_KEY]
    secret_items = [i for i in native["loss_report"] if i["kind"] == "secrets"]
    assert len(secret_items) == 1
    assert secret_items[0]["carried_in_extension"] is False
    assert scan_card(secret_items[0]["detail"].encode(), _CONTENT) == []

    async with tenant_scope(tenant_id) as session:
        assert (
            await session.execute(select(Principal.id).where(Principal.id == author_id))
        ).scalar_one() == author_id
