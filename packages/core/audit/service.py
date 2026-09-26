"""AuditService: the one write path for ``audit_log`` (INV-6). Every row's hash
covers the previous row's hash, forming a per-tenant chain — a verifier
(``core.audit.verify``) can detect any row that was altered or deleted after the fact
without a matching chain break.

Concurrent appends for the *same* tenant are serialised with a Postgres advisory
transaction lock keyed on the tenant id, so two simultaneous ``append()`` calls can't both
read the same ``prev_hash`` and produce two rows claiming the same predecessor — that
would silently fork the chain and defeat the point of chaining it at all.

``append_in_session`` is the same logic taking a caller-supplied session instead
of opening its own ``tenant_scope()`` — for a call site (``core.overseer.service
.inspect``, INV-5) that must commit the audit row in the *same* transaction as the read
it documents, not a second, independently-committing one. ``append()`` is unchanged and
still the right call for every site that doesn't need that.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit.hashing import audit_payload, compute_row_hash
from core.audit.models import AuditLogRow
from core.tenancy.scope import tenant_scope


async def _append_in_session(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_principal_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    target_ids: list[uuid.UUID],
    query: dict[str, object] | None,
    ip: str | None,
    user_agent: str | None,
) -> AuditLogRow:
    # Advisory lock scoped to this transaction: released automatically on
    # commit/rollback, serialises the read-prev/compute/insert sequence below
    # against any other append for the same tenant.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:tenant_id))"),
        {"tenant_id": str(tenant_id)},
    )

    prev_hash = await session.scalar(
        select(AuditLogRow.row_hash)
        .where(AuditLogRow.tenant_id == tenant_id)
        .order_by(AuditLogRow.created_at.desc(), AuditLogRow.id.desc())
        .limit(1)
    )

    payload = audit_payload(
        tenant_id=tenant_id,
        actor_principal_id=actor_principal_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        target_ids=target_ids,
        query=query,
    )
    row_hash = compute_row_hash(prev_hash, payload)

    row = AuditLogRow(
        tenant_id=tenant_id,
        actor_principal_id=actor_principal_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        target_ids=target_ids,
        query=query,
        ip=ip,
        user_agent=user_agent,
        prev_hash=prev_hash,
        row_hash=row_hash,
    )
    session.add(row)
    await session.flush()
    return row


class AuditService:
    async def append(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_principal_id: uuid.UUID,
        action: str,
        resource_type: str,
        resource_id: uuid.UUID | None = None,
        target_ids: list[uuid.UUID] | None = None,
        query: dict[str, object] | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> AuditLogRow:
        async with tenant_scope(tenant_id) as session:
            return await _append_in_session(
                session,
                tenant_id=tenant_id,
                actor_principal_id=actor_principal_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                target_ids=target_ids or [],
                query=query,
                ip=ip,
                user_agent=user_agent,
            )

    async def append_in_session(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        actor_principal_id: uuid.UUID,
        action: str,
        resource_type: str,
        resource_id: uuid.UUID | None = None,
        target_ids: list[uuid.UUID] | None = None,
        query: dict[str, object] | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> AuditLogRow:
        """Same hash-chain logic as ``append()``, but on the caller's own session/
        transaction -- the caller is responsible for that session already being scoped
        to ``tenant_id`` via ``tenant_scope()``; this function trusts, not re-derives,
        that scoping."""
        return await _append_in_session(
            session,
            tenant_id=tenant_id,
            actor_principal_id=actor_principal_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            target_ids=target_ids or [],
            query=query,
            ip=ip,
            user_agent=user_agent,
        )
