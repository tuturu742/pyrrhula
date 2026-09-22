"""Worker entrypoint. Run via ``python -m worker.main`` (docker/entrypoint.sh).

Same image as the API, different command. Claims jobs via the ``JobQueue`` port (Postgres
``SELECT ... FOR UPDATE SKIP LOCKED``, T0.3) and dispatches by ``kind`` to a handler
registered in ``_HANDLERS`` — this finishes what T0.3's stub docstring promised ("T0.3
replaces this with JobQueue.poll()") but never actually landed; A1.2 is the first task
that needs a real out-of-process job to run.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from adapters.queue.postgres.queue import PostgresJobQueue
from core.observability.otel import configure_tracing
from core.ports.job_queue import Job, JobQueue
from worker.delegation import (
    handle_delegate_work_item,
    handle_kill_exec_environment,
    handle_rework_work_item,
    handle_teardown_session_envs,
)
from worker.embedding import handle_embed_chunks, handle_reembed_stale
from worker.export import handle_export_workspace
from worker.history_summary import handle_summarise_history
from worker.ingestion import handle_knowledge_ingest
from worker.notifications import handle_notify_await_opened, handle_send_digest
from worker.pr_sync import sync_pull_requests
from worker.preview import handle_start_preview, handle_stop_preview, reap_expired_previews
from worker.render_reports import handle_render_report
from worker.repo_analysis import handle_analyze_workspace_repos
from worker.repo_ingest import handle_ingest_repo_snapshot
from worker.reports import handle_generate_report
from worker.retrieval_models import handle_download_retrieval_models
from worker.review import handle_facilitator_review, handle_merge_order
from worker.schedules import handle_apply_due_schedules

log = structlog.get_logger()

_IDLE_POLL_INTERVAL = 1.0

_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "knowledge_ingest": handle_knowledge_ingest,
    "embed_chunks": handle_embed_chunks,
    "reembed_stale": handle_reembed_stale,
    "summarise_history": handle_summarise_history,
    "apply_due_schedules": handle_apply_due_schedules,
    "notify_await_opened": handle_notify_await_opened,
    "send_digest": handle_send_digest,
    "export_workspace": handle_export_workspace,
    "generate_report": handle_generate_report,
    "render_report": handle_render_report,
    "ingest_repo_snapshot": handle_ingest_repo_snapshot,
    "analyze_workspace_repos": handle_analyze_workspace_repos,
    "delegate_work_item": handle_delegate_work_item,
    "rework_work_item": handle_rework_work_item,
    "facilitator_review": handle_facilitator_review,
    "merge_order": handle_merge_order,
    "teardown_session_envs": handle_teardown_session_envs,
    "kill_exec_environment": handle_kill_exec_environment,
    "start_preview": handle_start_preview,
    "download_retrieval_models": handle_download_retrieval_models,
    "stop_preview": handle_stop_preview,
}

# Previews are the only thing here with a wall-clock deadline, and this deployment has no
# scheduler -- every other recurring-looking job is enqueued by an HTTP request. So the
# idle branch doubles as the tick. Kubernetes also enforces its own deadline
# (activeDeadlineSeconds), which covers the case where the worker itself is down.
_REAP_INTERVAL = 60.0


async def _run_one(queue: JobQueue, job: Job) -> None:
    handler = _HANDLERS[job.kind]
    log.info("worker.job_claimed", job_id=str(job.id), kind=job.kind, tenant_id=str(job.tenant_id))
    try:
        result = await handler(job.payload)
    except Exception as exc:
        log.warning("worker.job_failed", job_id=str(job.id), kind=job.kind, error=str(exc))
        await queue.fail(job.id, str(exc))
        return
    log.info("worker.job_completed", job_id=str(job.id), kind=job.kind)
    await queue.complete(job.id, result)


async def main() -> None:
    configure_tracing(service_name="pyrrhula-worker")
    try:
        from core.deployment_settings import apply_retrieval_override_to_settings

        applied = await apply_retrieval_override_to_settings()
        if applied:
            log.info("retrieval.override_applied", model=applied.get("embedding_model"))
    except Exception as exc:  # noqa: BLE001 -- never block startup on an optional override
        log.warning("retrieval.override_failed", error=str(exc)[:300])
    log.info("worker.startup")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    queue: JobQueue = PostgresJobQueue()
    next_reap = asyncio.get_running_loop().time()

    while not stop.is_set():
        job = await queue.claim_one(kinds=list(_HANDLERS))
        if job is None:
            now = asyncio.get_running_loop().time()
            if now >= next_reap:
                next_reap = now + _REAP_INTERVAL
                try:
                    reaped = await reap_expired_previews()
                    if reaped:
                        log.info("worker.previews_reaped", count=reaped)
                except Exception as exc:  # noqa: BLE001 -- a sweep must never kill the loop
                    log.warning("worker.reap_failed", error=str(exc))
                # What the host did to a pull request has to come back: merged
                # elsewhere, or closed without merging, the work item followed
                # neither and sat in review forever.
                try:
                    followed = await sync_pull_requests()
                    if followed:
                        log.info("worker.work_items_followed_prs", count=followed)
                except Exception as exc:  # noqa: BLE001 -- a sweep must never kill the loop
                    log.warning("worker.pr_sync_failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=_IDLE_POLL_INTERVAL)
            continue
        await _run_one(queue, job)

    log.info("worker.shutdown")


if __name__ == "__main__":
    asyncio.run(main())
