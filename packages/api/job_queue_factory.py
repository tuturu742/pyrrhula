"""The api composition root for ``JobQueue`` selection (A1.2) — v1 is always the
Postgres SKIP LOCKED adapter (T0.3); Temporal (H5.9) is a swap behind the same port.
"""

from __future__ import annotations

from adapters.queue.postgres.queue import PostgresJobQueue
from core.ports.job_queue import JobQueue

_job_queue: JobQueue | None = None


def get_job_queue() -> JobQueue:
    global _job_queue
    if _job_queue is None:
        _job_queue = PostgresJobQueue()
    return _job_queue
