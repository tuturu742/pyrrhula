"""the worker-side job handler wiring (BlobStore fetch + idempotency + the real
pipeline), exercised against a live Postgres and a temp-dir BlobStore.
"""

from __future__ import annotations

import uuid

from adapters.blob.local.filesystem import LocalFilesystemBlobStore
from core.knowledge.authoring import create_source, list_draft_entries
from core.tenancy.seed import seed_dev_tenant
from worker import blob_store_factory, ingestion


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    return tenant_id, source.id


async def test_handle_knowledge_ingest_end_to_end(db_available: None, tmp_path) -> None:  # noqa: ANN001
    store = LocalFilesystemBlobStore(tmp_path)
    blob_store_factory._blob_store = store  # composition-root singleton, reset per test
    try:
        tenant_id, source_id = await _setup("worker-ing")
        blob_key = "uploads/core-rules.md"
        await store.put(
            blob_key, b"# Grappling\nRoll 1d20+STR to grapple.\n", content_type="text/markdown"
        )

        result = await ingestion.handle_knowledge_ingest(
            {
                "tenant_id": str(tenant_id),
                "knowledge_source_id": str(source_id),
                "blob_key": blob_key,
                "filename": "core-rules.md",
                "class": "rules",
                "scope_key": "workspace_public",
            }
        )

        assert result["entries_created"] == 1
        assert result["chunks_created"] == 1

        entries = await list_draft_entries(tenant_id, source_id)
        assert {e.entry_key for e in entries} == {"grappling"}
    finally:
        blob_store_factory._blob_store = None


async def test_handle_knowledge_ingest_is_idempotent_on_rerun(db_available: None, tmp_path) -> None:  # noqa: ANN001
    store = LocalFilesystemBlobStore(tmp_path)
    blob_store_factory._blob_store = store
    try:
        tenant_id, source_id = await _setup("worker-ing-idem")
        blob_key = "uploads/core-rules.md"
        await store.put(blob_key, b"# Grappling\nRoll 1d20+STR.\n", content_type="text/markdown")

        payload = {
            "tenant_id": str(tenant_id),
            "knowledge_source_id": str(source_id),
            "blob_key": blob_key,
            "filename": "core-rules.md",
            "class": "rules",
            "scope_key": "workspace_public",
        }

        first = await ingestion.handle_knowledge_ingest(payload)
        # Re-running the *same job* (same idempotency key: tenant+blob_key) must return
        # the cached result rather than re-running the pipeline a second time.
        second = await ingestion.handle_knowledge_ingest(payload)
        assert first == second
    finally:
        blob_store_factory._blob_store = None
