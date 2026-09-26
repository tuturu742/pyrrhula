"""OverseerService (E2.10, INV-1, INV-5, Q6): the only other secret-plaintext
read path besides `ContextAssembler.assemble()`. `inspect()` is one transaction --
permission check, read, and audit append, or none of it. A forced failure between the
read and the append must leave neither visible (this task's own acceptance criterion) --
there is no helper anywhere in this module that reads without auditing in the same
transaction.

**The overseer is a principal type with a permission, not a scope** : in-fiction
rules (`scope_key`) never gate this service; only `secret:inspect` does. One service for
both consuming surfaces (the Director's View UI, E2.11, and the `overseer.query` MCP
tool, E2.12) -- two audit paths would drift, and one of them would lie.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from core.audit.service import AuditService
from core.ports.encryptor import Encryptor
from core.ports.permission import PermissionService, UnknownActionError
from core.secrets.models import SecretDisclosureEventRow, SecretHolderRow, SecretRow
from core.tenancy.scope import tenant_scope

_INSPECT_ACTION = "secret:inspect"
_RESOURCE_TYPE = "secret"


class OverseerPermissionDeniedError(Exception):
    pass


class SecretNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class OverseerSecretView:
    id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    content: str
    gist: str
    hint_text: str | None
    behavioral_directive: str | None
    disclosure_state: str


@dataclass(frozen=True)
class HolderSummary:
    secret_id: uuid.UUID
    holder_principal_id: uuid.UUID
    holder_kind: str


@dataclass(frozen=True)
class DisclosureEventSummary:
    secret_id: uuid.UUID
    session_id: uuid.UUID
    event_seq: int
    mode: str
    disclosed_to: dict[str, object]


@dataclass(frozen=True)
class AgentBelief:
    """ "What does agent X currently hold/believe" -- a secret the agent holds, and
    whether it's been disclosed (to anyone) yet, from the agent's own holder row plus
    the secret's own `disclosure_state`."""

    secret_id: uuid.UUID
    holder_kind: str
    disclosure_state: str


class OverseerService:
    def __init__(
        self,
        *,
        encryptor: Encryptor,
        permission_service: PermissionService,
        audit_service: AuditService | None = None,
    ) -> None:
        self._encryptor = encryptor
        self._permission_service = permission_service
        self._audit = audit_service or AuditService()

    async def _require_inspect(
        self, tenant_id: uuid.UUID, principal_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> None:
        try:
            granted = await self._permission_service.check(
                tenant_id, principal_id, _INSPECT_ACTION, "workspace", workspace_id
            )
        except UnknownActionError:
            granted = False
        if not granted:
            raise OverseerPermissionDeniedError(
                f"principal {principal_id} lacks {_INSPECT_ACTION!r} on workspace {workspace_id}"
            )

    async def inspect(
        self,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
        workspace_id: uuid.UUID,
        secret_id: uuid.UUID,
        *,
        query: dict[str, object] | None = None,
    ) -> OverseerSecretView:
        """The plaintext read. Scope rules never apply here -- only the permission
        check does, and it runs *before* the transaction that reads and audits, so a
        denied principal never even opens a read transaction against `secret`."""
        await self._require_inspect(tenant_id, principal_id, workspace_id)

        async with tenant_scope(tenant_id) as session:
            row = await session.get(SecretRow, secret_id)
            if row is None:
                raise SecretNotFoundError(f"no secret {secret_id}")

            content = self._encryptor.decrypt(row.content_ciphertext)

            # Same transaction as the read above -- append_in_session, not append():
            # a caller must never be able to observe a read that has no corresponding
            # audit row, and vice versa.
            await self._audit.append_in_session(
                session,
                tenant_id=tenant_id,
                actor_principal_id=principal_id,
                action=_INSPECT_ACTION,
                resource_type=_RESOURCE_TYPE,
                resource_id=secret_id,
                target_ids=[secret_id],
                query=query,
            )

            return OverseerSecretView(
                id=row.id,
                subject_kind=row.subject_kind,
                subject_id=row.subject_id,
                content=content,
                gist=row.gist,
                hint_text=row.hint_text,
                behavioral_directive=row.behavioral_directive,
                disclosure_state=row.disclosure_state,
            )

    async def list_holders(
        self,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
        workspace_id: uuid.UUID,
        secret_id: uuid.UUID,
    ) -> list[HolderSummary]:
        """Metadata only (no `content`) -- still permission-gated and audited, since
        who-knows-what is itself sensitive, but doesn't need an `Encryptor`."""
        await self._require_inspect(tenant_id, principal_id, workspace_id)
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    select(SecretHolderRow).where(SecretHolderRow.secret_id == secret_id)
                )
            ).scalars()
            holders = list(rows)
            await self._audit.append_in_session(
                session,
                tenant_id=tenant_id,
                actor_principal_id=principal_id,
                action=_INSPECT_ACTION,
                resource_type="secret_holder",
                resource_id=secret_id,
                target_ids=[secret_id],
            )
        return [
            HolderSummary(
                secret_id=h.secret_id,
                holder_principal_id=h.holder_principal_id,
                holder_kind=h.holder_kind,
            )
            for h in holders
        ]

    async def list_secrets_by_subject(
        self,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
        workspace_id: uuid.UUID,
        subject_kind: str,
        subject_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        await self._require_inspect(tenant_id, principal_id, workspace_id)
        async with tenant_scope(tenant_id) as session:
            ids = (
                await session.execute(
                    select(SecretRow.id).where(
                        SecretRow.tenant_id == tenant_id,
                        SecretRow.subject_kind == subject_kind,
                        SecretRow.subject_id == subject_id,
                    )
                )
            ).scalars()
            secret_ids = list(ids)
            await self._audit.append_in_session(
                session,
                tenant_id=tenant_id,
                actor_principal_id=principal_id,
                action=_INSPECT_ACTION,
                resource_type="secret",
                target_ids=secret_ids,
                query={"subject_kind": subject_kind, "subject_id": str(subject_id)},
            )
        return secret_ids

    async def disclosure_timeline(
        self,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
        workspace_id: uuid.UUID,
        secret_id: uuid.UUID,
    ) -> list[DisclosureEventSummary]:
        await self._require_inspect(tenant_id, principal_id, workspace_id)
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    select(SecretDisclosureEventRow)
                    .where(SecretDisclosureEventRow.secret_id == secret_id)
                    .order_by(SecretDisclosureEventRow.created_at)
                )
            ).scalars()
            events = list(rows)
            await self._audit.append_in_session(
                session,
                tenant_id=tenant_id,
                actor_principal_id=principal_id,
                action=_INSPECT_ACTION,
                resource_type="secret_disclosure_event",
                resource_id=secret_id,
                target_ids=[secret_id],
            )
        return [
            DisclosureEventSummary(
                secret_id=e.secret_id,
                session_id=e.session_id,
                event_seq=e.event_seq,
                mode=e.mode,
                disclosed_to=e.disclosed_to,
            )
            for e in events
        ]

    async def agent_beliefs(
        self,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
        workspace_id: uuid.UUID,
        agent_principal_id: uuid.UUID,
    ) -> list[AgentBelief]:
        """ "What does agent X currently hold/believe" -- holders ∩ disclosure state, per
        this task's own subtask."""
        await self._require_inspect(tenant_id, principal_id, workspace_id)
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    select(SecretHolderRow, SecretRow.disclosure_state)
                    .join(SecretRow, SecretRow.id == SecretHolderRow.secret_id)
                    .where(SecretHolderRow.holder_principal_id == agent_principal_id)
                )
            ).all()
            beliefs = [
                AgentBelief(
                    secret_id=holder.secret_id,
                    holder_kind=holder.holder_kind,
                    disclosure_state=disclosure_state,
                )
                for holder, disclosure_state in rows
            ]
            await self._audit.append_in_session(
                session,
                tenant_id=tenant_id,
                actor_principal_id=principal_id,
                action=_INSPECT_ACTION,
                resource_type="agent_beliefs",
                target_ids=[agent_principal_id],
            )
        return beliefs
