"""Scheduled-effect job handler -- the worker-side wiring
``core.entities.schedule`` needs but can't import itself (composition root: which
``PermissionService`` adapter, the same rule ``worker.ingestion`` follows). Registered in
``worker.main``'s dispatch table under kind ``"apply_due_schedules"``.

The job is deliberately re-drivable over a clock range rather than "apply the next tick":
every application is idempotent on ``(schedule, tick)`` (rule 8, through the
``mutate``), so a retry after a partial failure re-offers the whole range and lands only
what is genuinely still outstanding. That means the queue never has to guarantee
exactly-once delivery -- it only has to guarantee at-least-once, which is the one it can
actually keep.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any

from adapters.permission.role_permission import RolePermissionService
from core.entities.schedule import apply_due_schedules


async def handle_apply_due_schedules(payload: dict[str, Any]) -> dict[str, Any]:
    """Job-payload adapter: ``JobQueue.Job.payload`` is a plain JSON dict (ids arrive as
    strings). ``principal_id`` is the principal whose clock advance opened this range --
    the schedule effects are attributed to them, and the ``entity:mutate`` check runs
    against them, so a schedule can never grant a principal a write they couldn't make
    directly."""
    applied = await apply_due_schedules(
        uuid.UUID(payload["tenant_id"]),
        uuid.UUID(payload["workspace_id"]),
        uuid.UUID(payload["principal_id"]),
        int(payload["from_clock"]),
        int(payload["to_clock"]),
        permission_service=RolePermissionService(),
    )
    return {
        "applied": [
            {
                **asdict(effect),
                "schedule_id": str(effect.schedule_id),
                "entity_id": str(effect.entity_id),
            }
            for effect in applied
        ],
        "count": len(applied),
    }
