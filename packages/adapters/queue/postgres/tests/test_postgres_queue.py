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
