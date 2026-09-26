"""Ingestion job handler  — the worker-side wiring `core.knowledge.ingestion`
needs but can't import itself (composition root: which ``BlobStore``/``ModelProvider``
adapter, same rule ``core.process.skeleton`` follows). Registered in ``worker.main``'s job
dispatch table under kind ``"knowledge_ingest"``.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any

from core.actions.idempotency import idempotent
from core.knowledge.ingestion.pipeline import ingest_document
from worker.blob_store_factory import get_blob_store
from worker.job_queue_factory import get_job_queue
from worker.model_provider_factory import get_model_provider

# A generic, always-available model string for tokenizer selection only (the embedding
# job decides the real embedding model; ingestion just needs a stable, reasonable
# token-count estimate for chunk sizing). tiktoken's cl100k_base fallback applies for
# anything not literally an
# OpenAI model name — see LiteLLMModelProvider.count_tokens.
_TOKENIZER_MODEL = "gpt-4"


def _ingest_job_key(**kwargs: Any) -> str:
    return f"knowledge_ingest:{kwargs['tenant_id']}:{kwargs['blob_key']}"


@idempotent(key_fn=_ingest_job_key)
async def run_ingestion_job(
    *,
    tenant_id: uuid.UUID,
    knowledge_source_id: uuid.UUID,
    blob_key: str,
    filename: str,
    class_: str,
    scope_key: str,
) -> dict[str, Any]:
    blob_store = get_blob_store()
    data = await blob_store.get(blob_key)

    provider = get_model_provider("litellm")

    def count_tokens(text: str) -> int:
        return provider.count_tokens(text, _TOKENIZER_MODEL)

    stats = await ingest_document(
        tenant_id,
        knowledge_source_id,
        filename=filename,
        data=data,
        class_=class_,
        scope_key=scope_key,
        count_tokens=count_tokens,
    )

    # Chained job, not inline embedding: "ingestion job (parse/split/chunk) -> embedding
    # job " . Runs inside the idempotent-wrapped function so an
    # idempotent replay (cache hit, not a real re-run) never double-enqueues this.
    await get_job_queue().enqueue(
        tenant_id,
        "embed_chunks",
        {"tenant_id": str(tenant_id), "knowledge_source_id": str(knowledge_source_id)},
    )

    return asdict(stats)


async def handle_knowledge_ingest(payload: dict[str, Any]) -> dict[str, Any]:
    """Job-payload adapter: ``JobQueue.Job.payload`` is a plain JSON dict (tenant_id and
    knowledge_source_id arrive as strings), the pipeline wants real ``uuid.UUID``s."""
    return await run_ingestion_job(
        tenant_id=uuid.UUID(payload["tenant_id"]),
        knowledge_source_id=uuid.UUID(payload["knowledge_source_id"]),
        blob_key=payload["blob_key"],
        filename=payload["filename"],
        class_=payload["class"],
        scope_key=payload["scope_key"],
    )
