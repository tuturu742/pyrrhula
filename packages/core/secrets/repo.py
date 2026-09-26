"""The secrets repository — the only way `Secret`/`SecretHolder` plaintext (`content`,
`hint_text`, `behavioral_directive`) is read. **INV-1: only `core.assembler`
and `core.overseer` may import this module** — enforced by
`tests/architecture/test_inv1_import_graph.py`.

Authoring (create/edit a secret's four faces, holder management, AI-assisted drafting)
lives in `core.secrets.authoring`/`core.secrets.drafting` instead, freely importable —
mirroring `core.knowledge.authoring`'s exemption from the identical lint for
`core.knowledge.repo` (see that module's docstring). A human author editing their own
tenant's secret is not "stored text reaching a model" (INV-1's actual target); the one
call in `core.secrets.drafting` that *does* reach a model is a distinct, sanctioned
purpose (`'rewrite'`), scoped to content the author already possesses and consented
to send, not the disclosure/exclusion path this module's allowlist protects.

Persisting a `DisclosureDecision`/`SecretDisclosureEvent` lives in
`core.secrets.decisions` instead, also freely importable — writing one never reads
`content_ciphertext`, only already-computed gist-based judgments and ids, so it was never
really part of the plaintext-read surface this module's allowlist protects. That split
mirrors the authoring/repo one: `core/secrets/gate.py` needs to persist its own
output but must never be able to import this module, so the write path had to live
somewhere gate.py *can* reach.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.ports.encryptor import Encryptor
from core.secrets.models import SecretHolderRow, SecretRow
from core.tenancy.scope import tenant_scope


async def get_secret(tenant_id: uuid.UUID, secret_id: uuid.UUID) -> SecretRow | None:
    async with tenant_scope(tenant_id) as session:
        return await session.get(SecretRow, secret_id)


async def get_secret_plaintext(
    tenant_id: uuid.UUID, secret_id: uuid.UUID, *, encryptor: Encryptor
) -> str | None:
    """The one path that ever sees `content_ciphertext` decrypted for the in-session/
    overseer surface. Reserved for the overseer's audited reads (INV-5: the audit row for
    a plaintext read is written in the *same transaction* as the read) — the
    disclosure gate never calls this; it only ever sees `gist`."""
    row = await get_secret(tenant_id, secret_id)
    if row is None:
        return None
    return encryptor.decrypt(row.content_ciphertext)


async def list_holders(tenant_id: uuid.UUID, secret_id: uuid.UUID) -> list[SecretHolderRow]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(SecretHolderRow).where(SecretHolderRow.secret_id == secret_id)
            )
        ).scalars()
        return list(rows)


async def list_secrets_for_subject(
    tenant_id: uuid.UUID, subject_kind: str, subject_id: uuid.UUID
) -> list[SecretRow]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(SecretRow).where(
                    SecretRow.tenant_id == tenant_id,
                    SecretRow.subject_kind == subject_kind,
                    SecretRow.subject_id == subject_id,
                )
            )
        ).scalars()
        return list(rows)


# Re-exported so callers that only need the type, not the constructor, don't have to
# reach past this module into core.secrets.models (which INV-1 doesn't otherwise
# restrict, but there's no reason for anything but this module to touch it directly).
__all__ = [
    "SecretHolderRow",
    "SecretRow",
    "get_secret",
    "get_secret_plaintext",
    "list_holders",
    "list_secrets_for_subject",
]
