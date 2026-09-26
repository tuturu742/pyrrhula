"""Ingestion over real HTTP: upload a document (fast, no parsing in the api process), then
drive the enqueued job through the same handler the worker process would use, and check
the resulting chunks + job status.
"""

from __future__ import annotations

import time
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api import blob_store_factory
from api.job_queue_factory import get_job_queue
from api.main import app
from api.redis_client import get_redis
from core.tenancy.seed import seed_dev_tenant
from worker import blob_store_factory as worker_blob_store_factory
from worker.ingestion import handle_knowledge_ingest


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture(autouse=True)
def _isolated_blob_store(tmp_path):  # noqa: ANN001, ANN201
    """Api and worker each have their own BlobStore composition-root singleton (mirrors
    two separate processes in production) -- both need pointing at the same temp
    directory so a blob the api "process" writes is readable by the simulated worker
    call below."""
    from adapters.blob.local.filesystem import LocalFilesystemBlobStore

    store = LocalFilesystemBlobStore(tmp_path)
    blob_store_factory._blob_store = store
    worker_blob_store_factory._blob_store = store
    yield
    blob_store_factory._blob_store = None
    worker_blob_store_factory._blob_store = None


@pytest.fixture
def client():  # noqa: ANN201
    with TestClient(app) as test_client:
        yield test_client


def _register_and_login(client: TestClient, slug: str) -> str:
    email = f"{uuid.uuid4().hex}@example.com"
    response = client.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery", "display_name": "Tester"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


async def test_upload_enqueues_job_and_worker_processes_it(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"kn-ingest-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    token = _register_and_login(client, slug)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = client.post(
        "/knowledge/sources",
        json={"key": "core-rules", "name": "Core Rules", "class": "rules"},
        headers=headers,
    )
    source_id = create_resp.json()["id"]

    start = time.monotonic()
    ingest_resp = client.post(
        f"/knowledge/sources/{source_id}/ingest",
        files={"file": ("core-rules.md", b"# Grappling\nRoll 1d20+STR.\n", "text/markdown")},
        data={"class": "rules", "scope_key": "workspace_public"},
        headers=headers,
    )
    elapsed = time.monotonic() - start
    assert ingest_resp.status_code == 202, ingest_resp.text
    # The api process only writes a blob and enqueues a job -- no parsing/chunking here,
    # so this stays fast regardless of document size (the "200-page PDF" criterion).
    assert elapsed < 2.0
    job_id = uuid.UUID(ingest_resp.json()["job_id"])

    pending_status = client.get(f"/knowledge/jobs/{job_id}", headers=headers)
    assert pending_status.json()["status"] == "pending"

    # Simulate the worker: claim and run the job exactly as worker.main's loop would.
    job = await get_job_queue().claim_one(kinds=["knowledge_ingest"])
    assert job is not None
    assert job.id == job_id
    result = await handle_knowledge_ingest(job.payload)
    await get_job_queue().complete(job.id, result)

    done_status = client.get(f"/knowledge/jobs/{job_id}", headers=headers)
    assert done_status.json()["status"] == "done"
    assert done_status.json()["result"]["chunks_created"] == 1

    entries_resp = client.get(f"/knowledge/sources/{source_id}/entries", headers=headers)
    assert {e["entry_key"] for e in entries_resp.json()} == {"grappling"}


async def test_job_status_endpoint_requires_matching_tenant(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug_a = f"kn-ingest-a-{uuid.uuid4().hex[:8]}"
    slug_b = f"kn-ingest-b-{uuid.uuid4().hex[:8]}"
    tenant_a, _owner_a, _ws_a = await seed_dev_tenant(slug=slug_a)
    await seed_dev_tenant(slug=slug_b)

    # A harmless placeholder kind, not "knowledge_ingest" -- the job table has no RLS
    # (it's deliberately cross-tenant, core.ports.job_queue) and is shared across this
    # whole test run, so a real "knowledge_ingest" kind left permanently pending here
    # would be eligible for claim_one(kinds=["knowledge_ingest"]) in the other test in
    # this file, stealing its job. This test only exercises the tenant check on GET
    # /knowledge/jobs/{id}, which doesn't care what kind the job actually is.
    job_id = await get_job_queue().enqueue(tenant_a, "test_probe", {})

    token_b = _register_and_login(client, slug_b)
    resp = client.get(f"/knowledge/jobs/{job_id}", headers={"Authorization": f"Bearer {token_b}"})
    assert resp.status_code == 404


async def test_ingest_endpoint_requires_auth(client: TestClient, db_available: None) -> None:
    response = client.post(
        "/knowledge/sources/00000000-0000-0000-0000-000000000000/ingest",
        files={"file": ("x.md", b"# X\nY\n", "text/markdown")},
        data={"class": "rules", "scope_key": "workspace_public"},
    )
    assert response.status_code == 401
