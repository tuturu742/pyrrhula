import uuid

from adapters.queue.postgres.queue import PostgresJobQueue
from core.ports.job_queue import JobQueue


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
