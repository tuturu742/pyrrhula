"""Secret authoring: create/edit a secret's four faces and manage its
holder set. Freely importable — unlike `core.secrets.repo`, which INV-1 reserves for
`core.assembler`/`core.overseer` — mirroring `core.knowledge.authoring`'s identical
exemption from the same lint (see `core.secrets.repo`'s docstring for why).

Deliberately independent of `core.secrets.repo`: this module maps `core.secrets.models`
directly, the same way `core.knowledge.authoring` never imports `core.knowledge.repo`.
Some read/write surface genuinely overlaps (both modules can fetch a `SecretRow`) — that
duplication is the same deliberate trade knowledge authoring already made, not an
oversight.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from core.audit.service import AuditService
from core.ports.encryptor import Encryptor
from core.ports.moderation import ModerationProvider
from core.ports.permission import PermissionService, UnknownActionError
from core.secrets.models import SecretHolderRow, SecretRow
from core.tenancy.scope import tenant_scope

_AUTHOR_ACTION = "secret:author"
_MEMBER_ACTION = "view_workspace"
_PUBLICATION_VALUES = frozenset({"guarded", "publishable"})
_MODERATION_CONTEXT = "secret"

# Sentinel distinguishing "leave this field unchanged" from "set it to None" for the two
# nullable text faces — same shape as core.agents.authoring.update_agent's
# `api_base`, and for the same reason: `None` is a legal, meaningful value here ("clear
# the hint"/"clear the directive"), so omitted has to be spelled differently.
UNSET: object = object()


class SecretNotFoundError(Exception):
    pass


class SecretAccessDeniedError(Exception):
    """Raised on a write attempt (create/update/holder management) by a principal
    lacking `secret:author` on the secret's workspace. Reads never raise this — see
    `SecretView`, which degrades to a gist-only view instead (its own acceptance
    criterion: a participant's request for the same secret returns gist-level fields
    only, not a 403)."""


class SecretContentRejectedError(Exception):
    """Raised when the authoring-time moderation hook disallows `content` — the
    v1 `AllowAllModerationProvider` never actually raises this (it allows everything),
    but the call site exists now so the real provider is a pure adapter swap, not a
    retrofit touching every secret write path. Secret content is user-authored text like
    any other; the secrets model does not blind moderation."""


@dataclass(frozen=True)
class SecretView:
    """The read shape returned to any caller, author or not. `content`/`hint_text`/
    `behavioral_directive` are populated when the requester passes the `secret:author`
    check, or when the secret is `publishable` and they are a member of the workspace —
    `None` otherwise, never omitted-vs-present ambiguity.

    A publishable secret is one whose author has declared the plaintext shareable: a
    character brief written to be handed out. Reading one is not an overseer act, so it
    does not go through the audited `inspect()` path — publication is the sanction."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    gist: str
    disclosure_state: str
    scope_key: str
    publication: str
    version: int
    content: str | None
    hint_text: str | None
    behavioral_directive: str | None
    is_author: bool


async def _is_author(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    *,
    permission_service: PermissionService,
) -> bool:
    try:
        return await permission_service.check(
            tenant_id, principal_id, _AUTHOR_ACTION, "workspace", workspace_id
        )
    except UnknownActionError:
        return False


async def _is_workspace_member(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    *,
    permission_service: PermissionService,
) -> bool:
    """`view_workspace` is held by every workspace role (facilitator, overseer,
    participant, viewer) and by no tenant-only role, so it is the membership question
    asked through the port rather than by inlining a role lookup (CLAUDE.md rule 12)."""
    try:
        return await permission_service.check(
            tenant_id, principal_id, _MEMBER_ACTION, "workspace", workspace_id
        )
    except UnknownActionError:
        return False


async def create_secret(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    requesting_principal_id: uuid.UUID,
    *,
    subject_kind: str,
    subject_id: uuid.UUID,
    content: str,
    gist: str,
    scope_key: str,
    encryptor: Encryptor,
    permission_service: PermissionService,
    moderation_provider: ModerationProvider,
    hint_text: str | None = None,
    behavioral_directive: str | None = None,
    publication: str = "guarded",
) -> SecretRow:
    """`content` is encrypted before it ever touches a row  — the plaintext argument
    itself is not retained anywhere past this call returning.

    `publication` defaults to `guarded`: a secret is publishable only because someone
    said so, never because a caller forgot to say otherwise."""
    if not await _is_author(
        tenant_id, workspace_id, requesting_principal_id, permission_service=permission_service
    ):
        raise SecretAccessDeniedError(
            f"principal {requesting_principal_id} may not author secrets in workspace "
            f"{workspace_id}"
        )
    if publication not in _PUBLICATION_VALUES:
        raise ValueError(f"publication must be one of {sorted(_PUBLICATION_VALUES)}")
    moderation = await moderation_provider.check(content, context=_MODERATION_CONTEXT)
    if not moderation.allowed:
        raise SecretContentRejectedError(", ".join(moderation.reasons) or "content rejected")
    async with tenant_scope(tenant_id) as session:
        row = SecretRow(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            content_ciphertext=encryptor.encrypt(content),
            gist=gist,
            hint_text=hint_text,
            behavioral_directive=behavioral_directive,
            scope_key=scope_key,
            publication=publication,
            authored_by=requesting_principal_id,
        )
        session.add(row)
        await session.flush()
        # Trust-ops: secret authorship is auditable, in the same transaction as the row
        # (the log is the record of who created what a persona now knows).
        await AuditService().append_in_session(
            session,
            tenant_id=tenant_id,
            actor_principal_id=requesting_principal_id,
            action="secret:create",
            resource_type="secret",
            resource_id=row.id,
        )
        await session.refresh(row)
        return row


async def _row_to_view(
    tenant_id: uuid.UUID,
    row: SecretRow,
    requesting_principal_id: uuid.UUID,
    *,
    encryptor: Encryptor,
    permission_service: PermissionService,
) -> SecretView:
    is_author = await _is_author(
        tenant_id, row.workspace_id, requesting_principal_id, permission_service=permission_service
    )
    # A published brief is readable by the people running the table. This widens who may
    # read it as an *operator*; it changes nothing about what reaches an agent's context,
    # which is holder-gated in the assembler and stays that way (INV-8).
    may_read = is_author or (
        row.publication == "publishable"
        and await _is_workspace_member(
            tenant_id,
            row.workspace_id,
            requesting_principal_id,
            permission_service=permission_service,
        )
    )
    return SecretView(
        id=row.id,
        workspace_id=row.workspace_id,
        subject_kind=row.subject_kind,
        subject_id=row.subject_id,
        gist=row.gist,
        disclosure_state=row.disclosure_state,
        scope_key=row.scope_key,
        publication=row.publication,
        version=row.version,
        content=encryptor.decrypt(row.content_ciphertext) if may_read else None,
        hint_text=row.hint_text if may_read else None,
        behavioral_directive=row.behavioral_directive if may_read else None,
        is_author=is_author,
    )


async def get_secret_view(
    tenant_id: uuid.UUID,
    secret_id: uuid.UUID,
    requesting_principal_id: uuid.UUID,
    *,
    encryptor: Encryptor,
    permission_service: PermissionService,
) -> SecretView:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SecretRow, secret_id)
    if row is None:
        raise SecretNotFoundError(f"no secret {secret_id}")
    return await _row_to_view(
        tenant_id,
        row,
        requesting_principal_id,
        encryptor=encryptor,
        permission_service=permission_service,
    )


async def list_secret_views_for_workspace(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    requesting_principal_id: uuid.UUID,
    *,
    encryptor: Encryptor,
    permission_service: PermissionService,
) -> list[SecretView]:
    async with tenant_scope(tenant_id) as session:
        result = await session.execute(
            select(SecretRow).where(SecretRow.workspace_id == workspace_id)
        )
        rows = list(result.scalars())
    return [
        await _row_to_view(
            tenant_id,
            row,
            requesting_principal_id,
            encryptor=encryptor,
            permission_service=permission_service,
        )
        for row in rows
    ]


async def update_secret_fields(
    tenant_id: uuid.UUID,
    secret_id: uuid.UUID,
    requesting_principal_id: uuid.UUID,
    *,
    encryptor: Encryptor,
    permission_service: PermissionService,
    moderation_provider: ModerationProvider,
    content: str | None = None,
    gist: str | None = None,
    hint_text: str | None | object = UNSET,
    behavioral_directive: str | None | object = UNSET,
    publication: str | None = None,
) -> SecretRow:
    """Any accepted edit — manual or an accepted AI-assist draft — bumps `version` (the
    schema; there is no separate version-history table for secrets, unlike knowledge
    sources). `content`/`gist` use "omitted (`None`) means unchanged" (neither can
    legally be cleared to empty); `hint_text`/`behavioral_directive` use the `UNSET`
    sentinel since `None` is themselves a legal cleared value."""
    if content is not None:
        moderation = await moderation_provider.check(content, context=_MODERATION_CONTEXT)
        if not moderation.allowed:
            raise SecretContentRejectedError(", ".join(moderation.reasons) or "content rejected")
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SecretRow, secret_id)
        if row is None:
            raise SecretNotFoundError(f"no secret {secret_id}")
        if not await _is_author(
            tenant_id,
            row.workspace_id,
            requesting_principal_id,
            permission_service=permission_service,
        ):
            raise SecretAccessDeniedError(
                f"principal {requesting_principal_id} may not edit secret {secret_id}"
            )

        changed = False
        if content is not None:
            row.content_ciphertext = encryptor.encrypt(content)
            changed = True
        if gist is not None:
            row.gist = gist
            changed = True
        if hint_text is not UNSET:
            row.hint_text = hint_text  # type: ignore[assignment]
            changed = True
        if behavioral_directive is not UNSET:
            row.behavioral_directive = behavioral_directive  # type: ignore[assignment]
            changed = True
        if publication is not None and publication != row.publication:
            if publication not in _PUBLICATION_VALUES:
                raise ValueError(f"publication must be one of {sorted(_PUBLICATION_VALUES)}")
            row.publication = publication
            changed = True
        if changed:
            row.version += 1
            await AuditService().append_in_session(
                session,
                tenant_id=tenant_id,
                actor_principal_id=requesting_principal_id,
                action="secret:update",
                resource_type="secret",
                resource_id=row.id,
            )
        await session.flush()
        await session.refresh(row)
        return row


async def add_holder(
    tenant_id: uuid.UUID,
    secret_id: uuid.UUID,
    requesting_principal_id: uuid.UUID,
    holder_principal_id: uuid.UUID,
    holder_kind: str,
    *,
    permission_service: PermissionService,
    acquired_via_event_id: uuid.UUID | None = None,
) -> SecretHolderRow:
    async with tenant_scope(tenant_id) as session:
        secret = await session.get(SecretRow, secret_id)
        if secret is None:
            raise SecretNotFoundError(f"no secret {secret_id}")
        if not await _is_author(
            tenant_id,
            secret.workspace_id,
            requesting_principal_id,
            permission_service=permission_service,
        ):
            raise SecretAccessDeniedError(
                f"principal {requesting_principal_id} may not manage holders of secret {secret_id}"
            )
        row = SecretHolderRow(
            tenant_id=tenant_id,
            secret_id=secret_id,
            holder_principal_id=holder_principal_id,
            holder_kind=holder_kind,
            acquired_via_event_id=acquired_via_event_id,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


async def remove_holder(
    tenant_id: uuid.UUID,
    secret_id: uuid.UUID,
    requesting_principal_id: uuid.UUID,
    holder_id: uuid.UUID,
    *,
    permission_service: PermissionService,
) -> None:
    async with tenant_scope(tenant_id) as session:
        secret = await session.get(SecretRow, secret_id)
        if secret is None:
            raise SecretNotFoundError(f"no secret {secret_id}")
        if not await _is_author(
            tenant_id,
            secret.workspace_id,
            requesting_principal_id,
            permission_service=permission_service,
        ):
            raise SecretAccessDeniedError(
                f"principal {requesting_principal_id} may not manage holders of secret {secret_id}"
            )
        holder = await session.get(SecretHolderRow, holder_id)
        if holder is not None and holder.secret_id == secret_id:
            await session.delete(holder)


async def list_holders(tenant_id: uuid.UUID, secret_id: uuid.UUID) -> list[SecretHolderRow]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(SecretHolderRow).where(SecretHolderRow.secret_id == secret_id)
            )
        ).scalars()
        return list(rows)
