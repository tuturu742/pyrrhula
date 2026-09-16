"""v1 JobQueue: Postgres, ``SELECT ... FOR UPDATE SKIP LOCKED``. See
packages/core/ports/job_queue.py for why this table has no RLS."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import and_, or_, select, text, update

from adapters.queue.postgres.models import JobRow
from core.ports.job_queue import Job
from core.tenancy.scope import unscoped_session

# A worker that dies mid-job (OOM kill, node eviction, SIGKILL) leaves its row in
# `claimed` with no one running it. Without a lease, `claim_one`'s `status == "pending"`
# filter means that row is never looked at again: the work is not retried, not failed, and
# not reported -- it is silently lost. That is how a container OOM turned into "embeddings
# just never happened" with a green-looking queue.
_DEFAULT_LEASE_SECONDS = 900
# The other half: a job that reliably kills its worker would otherwise be reclaimed
# forever, taking the worker (and every other tenant's queued work, since the worker claims
# across tenants) down on each pass. After this many attempts the row is failed and left
# alone -- a poison job should be visible and inert, not a crash loop.
_DEFAULT_MAX_ATTEMPTS = 3


class PostgresJobQueue:
    def __init__(
        self,
        *,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts

    async def enqueue(self, tenant_id: uuid.UUID, kind: str, payload: dict[str, Any]) -> uuid.UUID:
        async with unscoped_session() as session:
            row = JobRow(tenant_id=tenant_id, kind=kind, payload=payload)
            session.add(row)
            await session.flush()
            return row.id

    async def claim_one(self, kinds: list[str] | None = None) -> Job | None:
        async with unscoped_session() as session:
            stale = JobRow.claimed_at < text(
                "now() - make_interval(secs => :lease_seconds)"
            ).bindparams(lease_seconds=self._lease_seconds)

            # Retire poison jobs before looking for work, so a job that kills its worker
            # stops being handed back out once it has had its attempts.
            await session.execute(
                update(JobRow)
                .where(
                    JobRow.status == "claimed",
                    stale,
                    JobRow.attempts >= self._max_attempts,
                )
                .values(
                    status="failed",
                    error=(
                        "abandoned: claimed but never completed after "
                        f"{self._max_attempts} attempts (worker died mid-job?)"
                    ),
                    completed_at=text("now()"),
                )
            )

            candidate_stmt = (
                select(JobRow.id)
                .where(
                    or_(
                        JobRow.status == "pending",
                        and_(JobRow.status == "claimed", stale),
                    )
                )
                .order_by(JobRow.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if kinds:
                candidate_stmt = candidate_stmt.where(JobRow.kind.in_(kinds))

            candidate_id = await session.scalar(candidate_stmt)
            if candidate_id is None:
                return None

            result = await session.execute(
                update(JobRow)
                .where(JobRow.id == candidate_id)
                .values(status="claimed", claimed_at=text("now()"), attempts=JobRow.attempts + 1)
                .returning(JobRow)
            )
            row = result.scalar_one()
            return Job(
                id=row.id,
                tenant_id=row.tenant_id,
                kind=row.kind,
                payload=row.payload,
                attempts=row.attempts,
                status=row.status,
                result=row.result,
                error=row.error,
            )

    async def complete(self, job_id: uuid.UUID, result: dict[str, Any] | None = None) -> None:
        async with unscoped_session() as session:
            await session.execute(
                update(JobRow)
                .where(JobRow.id == job_id)
                .values(status="done", result=result, completed_at=text("now()"))
            )

    async def fail(self, job_id: uuid.UUID, error: str) -> None:
        async with unscoped_session() as session:
            await session.execute(
                update(JobRow)
                .where(JobRow.id == job_id)
                .values(status="failed", error=error, completed_at=text("now()"))
            )

    async def get(self, job_id: uuid.UUID) -> Job | None:
        async with unscoped_session() as session:
            row = await session.get(JobRow, job_id)
        if row is None:
            return None
        return Job(
            id=row.id,
            tenant_id=row.tenant_id,
            kind=row.kind,
            payload=row.payload,
            attempts=row.attempts,
            status=row.status,
            result=row.result,
            error=row.error,
        )
