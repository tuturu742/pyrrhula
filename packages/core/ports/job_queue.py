"""JobQueue port. v1 is a Postgres table polled with
``SELECT ... FOR UPDATE SKIP LOCKED``; Temporal is a swap behind this port, done
only if operational data says the Postgres queue is inadequate — not on principle.

Jobs are cross-tenant, system/operational data (a worker must be able to claim work for
*any* tenant), so — like ``tenant`` and ``role_permission`` — the ``job`` table carries no
RLS policy; ``tenant_id`` is a plain column, not an isolation boundary here. Once a job is
claimed and its handler runs, that handler does its actual tenant-scoped work through
``tenant_scope(job.tenant_id)`` as usual.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Job:
    id: uuid.UUID
    tenant_id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    status: str = "claimed"
    result: dict[str, Any] | None = None
    error: str | None = None


class JobQueue(Protocol):
    async def enqueue(
        self, tenant_id: uuid.UUID, kind: str, payload: dict[str, Any]
    ) -> uuid.UUID: ...

    async def claim_one(self, kinds: list[str] | None = None) -> Job | None: ...

    async def heartbeat(self, job_id: uuid.UUID, *, attempts: int) -> bool:
        """Say "still working" so the claim lease measures silence, not duration.

        The lease exists to recover jobs whose worker died (see the adapter). Without a
        heartbeat it cannot tell a dead worker from a slow one, so any handler that runs
        longer than the lease is handed to a *second* worker while the first is still
        going: duplicate model spend, and two workers pushing the same branch.

        ``attempts`` is the generation the caller claimed at. A reclaim bumps it, so a
        heartbeat from a worker that has already lost the job returns ``False`` instead of
        extending the new holder's lease.
        """
        ...

    async def complete(self, job_id: uuid.UUID, result: dict[str, Any] | None = None) -> None: ...

    async def fail(self, job_id: uuid.UUID, error: str) -> None: ...

    async def get(self, job_id: uuid.UUID) -> Job | None:
        """Read-only status lookup (A1.2: job progress surfaced via API). Callers that
        expose this over HTTP must check ``tenant_id`` themselves — the ``job`` table
        carries no RLS (see this module's docstring), so this method deliberately
        doesn't scope by tenant either; unscoped-by-design, not an oversight."""
        ...
