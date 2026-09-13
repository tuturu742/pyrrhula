"""v1 JobQueue: Postgres, ``SELECT ... FOR UPDATE SKIP LOCKED``. See
packages/core/ports/job_queue.py for why this table has no RLS."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, text, update

from adapters.queue.postgres.models import JobRow
from core.ports.job_queue import Job
from core.tenancy.scope import unscoped_session


class PostgresJobQueue:
    async def enqueue(self, tenant_id: uuid.UUID, kind: str, payload: dict[str, Any]) -> uuid.UUID:
        async with unscoped_session() as session:
            row = JobRow(tenant_id=tenant_id, kind=kind, payload=payload)
            session.add(row)
            await session.flush()
            return row.id

    async def claim_one(self, kinds: list[str] | None = None) -> Job | None:
        async with unscoped_session() as session:
            candidate_stmt = (
                select(JobRow.id)
                .where(JobRow.status == "pending")
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
