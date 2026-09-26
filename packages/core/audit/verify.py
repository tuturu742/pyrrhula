"""Chain verifier ( layer 2): walks a tenant's audit log in order and checks two
things per row — its own hash is consistent with its recorded content and predecessor
(catches a row edited in place), and its ``prev_hash`` matches the actual previous row's
``row_hash`` (catches a row deleted or inserted out of order). Either failure is a
"chain break".

Runs standalone here (returns broken row ids; callers decide how to alert) — the actual
nightly worker job that calls this and pages someone is worker/job infrastructure to be
wired up alongside the rest of the job loop, not duplicated here.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.audit.hashing import audit_payload, compute_row_hash
from core.audit.models import AuditLogRow
from core.tenancy.scope import tenant_scope


async def verify_chain(tenant_id: uuid.UUID) -> list[uuid.UUID]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(AuditLogRow)
                    .where(AuditLogRow.tenant_id == tenant_id)
                    .order_by(AuditLogRow.created_at, AuditLogRow.id)
                )
            )
            .scalars()
            .all()
        )

    broken: list[uuid.UUID] = []
    expected_prev: str | None = None
    for row in rows:
        payload = audit_payload(
            tenant_id=row.tenant_id,
            actor_principal_id=row.actor_principal_id,
            action=row.action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            target_ids=list(row.target_ids),
            query=row.query,
        )
        expected_hash = compute_row_hash(row.prev_hash, payload)

        if expected_hash != row.row_hash or row.prev_hash != expected_prev:
            broken.append(row.id)

        expected_prev = row.row_hash

    return broken
