"""The worker's composition root for ``JobQueue`` selection — mirrors
``api.job_queue_factory`` (needed so the ingestion job can enqueue the follow-up
embedding job it chains to).
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
