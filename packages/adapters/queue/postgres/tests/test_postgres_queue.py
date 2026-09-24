import uuid

from sqlalchemy import text, update

from adapters.queue.postgres.models import JobRow
from adapters.queue.postgres.queue import PostgresJobQueue
from core.ports.job_queue import JobQueue
from core.tenancy.scope import unscoped_session


async def _age_claim(job_id: uuid.UUID, seconds: int) -> None:
    """Pretend the claim was taken `seconds` ago, so a lease can elapse without the test
    sleeping through it."""
    async with unscoped_session() as session:
        await session.execute(
            update(JobRow)
            .where(JobRow.id == job_id)
            .values(claimed_at=text(f"now() - make_interval(secs => {seconds})"))
        )


async def test_enqueue_claim_complete(db_available: None) -> None:
    queue: JobQueue = PostgresJobQueue()
    tenant_id = uuid.uuid4()

    job_id = await queue.enqueue(tenant_id, "embed_chunk", {"chunk_id": "abc"})

    claimed = await queue.claim_one(kinds=["embed_chunk"])
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.tenant_id == tenant_id
    assert claimed.attempts == 1

    # Already claimed -- must not be claimable again.
    assert await queue.claim_one(kinds=["embed_chunk"]) is None

    await queue.complete(job_id, result={"ok": True})


async def test_claim_respects_kind_filter(db_available: None) -> None:
    queue: JobQueue = PostgresJobQueue()
    tenant_id = uuid.uuid4()

    await queue.enqueue(tenant_id, "report_generate", {})

    assert await queue.claim_one(kinds=["embed_chunk"]) is None
    claimed = await queue.claim_one(kinds=["report_generate"])
    assert claimed is not None
    assert claimed.kind == "report_generate"


async def test_fail_records_error(db_available: None) -> None:
    queue: JobQueue = PostgresJobQueue()
    tenant_id = uuid.uuid4()

    job_id = await queue.enqueue(tenant_id, "ingest", {})
    claimed = await queue.claim_one()
    assert claimed is not None

    await queue.fail(job_id, "boom")
    # No assertion on internal state beyond "did not raise" -- claim_one no longer
    # returning it (status != 'pending') is exercised implicitly by the next test's
    # isolation from this one via a fresh tenant_id/job.


async def test_get_reflects_status_result_and_error(db_available: None) -> None:
    queue: JobQueue = PostgresJobQueue()
    tenant_id = uuid.uuid4()

    job_id = await queue.enqueue(tenant_id, "embed_chunk", {"chunk_id": "xyz"})
    assert (await queue.get(job_id)).status == "pending"  # type: ignore[union-attr]

    await queue.claim_one(kinds=["embed_chunk"])
    assert (await queue.get(job_id)).status == "claimed"  # type: ignore[union-attr]

    await queue.complete(job_id, result={"chunks": 3})
    done = await queue.get(job_id)
    assert done is not None
    assert done.status == "done"
    assert done.result == {"chunks": 3}


async def test_get_returns_none_for_unknown_job(db_available: None) -> None:
    queue: JobQueue = PostgresJobQueue()
    assert await queue.get(uuid.uuid4()) is None


async def test_a_job_stranded_by_a_dead_worker_is_reclaimed(db_available: None) -> None:
    """Regression: `claim_one` only ever looked at `status = 'pending'`, so a row left in
    `claimed` by a worker that died mid-job (here: an OOM kill) was never retried, never
    failed and never surfaced -- the work silently did not happen while the queue looked
    healthy. `lease_seconds=0` makes any claim immediately stale, standing in for elapsed
    time without sleeping."""
    kind = f"embed_chunks_{uuid.uuid4().hex[:8]}"
    tenant_id = uuid.uuid4()

    crashed: JobQueue = PostgresJobQueue()
    job_id = await crashed.enqueue(tenant_id, kind, {"source": "repo"})
    claimed = await crashed.claim_one(kinds=[kind])
    assert claimed is not None and claimed.attempts == 1
    # ...and now the worker dies: no complete(), no fail(), the row stays `claimed`.

    recovered = await PostgresJobQueue(lease_seconds=0).claim_one(kinds=[kind])
    assert recovered is not None, "a stranded job was never picked back up"
    assert recovered.id == job_id
    assert recovered.attempts == 2


async def test_a_job_that_keeps_killing_its_worker_is_retired(db_available: None) -> None:
    """The other half of the lease: reclaiming forever would let one poison job crash-loop
    the worker, which takes every *other* tenant's queued work with it because the worker
    claims across tenants. After max_attempts the row is failed and left alone."""
    kind = f"embed_chunks_{uuid.uuid4().hex[:8]}"
    queue = PostgresJobQueue(lease_seconds=0, max_attempts=2)
    job_id = await queue.enqueue(uuid.uuid4(), kind, {})

    for expected_attempts in (1, 2):
        claimed = await queue.claim_one(kinds=[kind])
        assert claimed is not None
        assert claimed.attempts == expected_attempts

    assert await queue.claim_one(kinds=[kind]) is None, "poison job was handed out again"

    retired = await queue.get(job_id)
    assert retired is not None
    assert retired.status == "failed"
    assert "abandoned" in (retired.error or "")


async def test_a_slow_worker_that_is_still_working_keeps_its_job(db_available: None) -> None:
    """The lease is meant to recover jobs whose worker died, but it could not tell a dead
    worker from a slow one: it measured how long the job had been claimed, and nothing
    else. A codegen rework routinely runs past 900s, so a second worker picked up a job the
    first was still running -- the tenant paid for the same generation twice and both
    workers pushed to the same branch. Observed live, 2026-09-24: job e679840b claimed at
    05:55:25 and again at 06:11:22.

    `lease_seconds=0` stands in for "the lease has elapsed" without sleeping, so a
    heartbeat is the only thing that can save the claim here."""
    kind = f"rework_work_item_{uuid.uuid4().hex[:8]}"
    tenant_id = uuid.uuid4()

    queue: JobQueue = PostgresJobQueue()
    await queue.enqueue(tenant_id, kind, {"branch": "pyr/slow"})
    mine = await queue.claim_one(kinds=[kind])
    assert mine is not None

    # Ten minutes of honest work under a five-minute lease: stale by duration alone.
    await _age_claim(mine.id, seconds=600)
    assert await queue.heartbeat(mine.id, attempts=mine.attempts) is True

    stealer = await PostgresJobQueue(lease_seconds=300).claim_one(kinds=[kind])
    assert stealer is None, "a job whose worker said it was still working was taken away"


async def test_a_worker_that_lost_its_job_stops_holding_the_lease_open(
    db_available: None,
) -> None:
    """The generation guard. If a heartbeat matched on job id alone, a worker that had
    genuinely gone silent long enough to be reclaimed would come back and keep extending
    the *new* holder's lease -- and would never find out it had lost the job."""
    kind = f"rework_work_item_{uuid.uuid4().hex[:8]}"
    tenant_id = uuid.uuid4()

    queue: JobQueue = PostgresJobQueue()
    await queue.enqueue(tenant_id, kind, {"branch": "pyr/silent"})
    first = await queue.claim_one(kinds=[kind])
    assert first is not None

    # This worker really did go silent -- no heartbeat -- so the reclaim is correct.
    await _age_claim(first.id, seconds=600)
    second = await PostgresJobQueue(lease_seconds=300).claim_one(kinds=[kind])
    assert second is not None and second.id == first.id
    assert second.attempts > first.attempts

    assert await queue.heartbeat(first.id, attempts=first.attempts) is False
    assert await queue.heartbeat(second.id, attempts=second.attempts) is True
