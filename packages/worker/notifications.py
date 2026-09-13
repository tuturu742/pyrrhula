"""Notification + digest job handlers (G4.3) -- the worker-side wiring
``core.sessions.notifications`` needs but can't import itself (composition root: which
``Notifier`` and which ``PermissionService`` adapter). Registered in ``worker.main``'s
dispatch table under ``"notify_await_opened"`` and ``"send_digest"``.

Both are safe to retry without any idempotency wrapper of their own: the notification
table's UNIQUE ``dedupe_key`` decides who actually sends, so a re-delivered job finds its
work already done and sends nothing. That is deliberate -- an ``@idempotent`` wrapper here
would add a *second* exactly-once mechanism over the one that already has to exist.
"""

from __future__ import annotations

import uuid
from typing import Any

from adapters.permission.role_permission import RolePermissionService
from core.process.dsl.schema import PhaseSpec
from core.sessions.notifications import build_digest, notify_await_opened, send_digest
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope
from worker.notifier_factory import get_notifier


async def handle_notify_await_opened(payload: dict[str, Any]) -> dict[str, Any]:
    rows = await notify_await_opened(
        uuid.UUID(payload["tenant_id"]),
        uuid.UUID(payload["workspace_id"]),
        uuid.UUID(payload["await_state_id"]),
        notifier=get_notifier(),
    )
    return {"notified": [str(row.principal_id) for row in rows], "count": len(rows)}


async def handle_send_digest(payload: dict[str, Any]) -> dict[str, Any]:
    """``window_key`` is the caller's periodicity marker (an ISO date for a daily digest,
    an ISO week for a weekly one). Keeping it in the payload rather than computing it here
    means "how often" is a scheduling decision, and re-running yesterday's digest job
    tomorrow does not silently produce a second copy of yesterday's digest."""
    tenant_id = uuid.UUID(payload["tenant_id"])
    async with tenant_scope(tenant_id) as session:
        viewer = await session.get(Principal, uuid.UUID(payload["principal_id"]))
        if viewer is None:
            raise ValueError(f"no principal {payload['principal_id']} in this tenant")
        session.expunge(viewer)

    digest = await build_digest(
        tenant_id,
        uuid.UUID(payload["workspace_id"]),
        viewer,
        PhaseSpec.model_validate(payload["phase"]),
        session_id=uuid.UUID(payload["session_id"]),
        from_event_seq=int(payload["from_event_seq"]),
        to_event_seq=int(payload["to_event_seq"]),
        permission_service=RolePermissionService(),
    )
    row = await send_digest(tenant_id, digest, str(payload["window_key"]), notifier=get_notifier())
    return {
        "sent": row is not None,
        "pending_count": len(digest.pending),
        "fact_count": len(digest.facts),
    }
