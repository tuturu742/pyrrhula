"""Fetching the retrieval models on demand, off the install path.

This runs in the worker because the worker shares the model cache volume with the api and
because a multi-gigabyte download must not hold an HTTP request open. It is the same
constructor call the providers make lazily, so it cannot pull a different set of files
than the runtime will look for.
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger()


async def handle_download_retrieval_models(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the configured models into the shared cache. Idempotent: an already-cached
    model is a no-op inside sentence-transformers, so a re-run costs a metadata check."""
    import asyncio

    from core.deployment_settings import get_retrieval_models
    from core.retrieval_cache import fetch_models

    effective = await get_retrieval_models()
    embedding = str(payload.get("embedding_model") or effective["embedding_model"])
    reranker = (
        str(payload.get("reranker_model") or effective["reranker_model"])
        if effective.get("reranker_enabled")
        else None
    )

    log.info("retrieval_models.download_started", embedding=embedding, reranker=reranker)
    try:
        # Blocking and long; off the event loop so the worker keeps its heartbeat.
        result = await asyncio.to_thread(fetch_models, embedding, reranker)
    except Exception as exc:  # noqa: BLE001 -- the outcome is data for the admin console
        log.warning("retrieval_models.download_failed", error=str(exc)[:300])
        return {"outcome": "failed", "error": str(exc)[:300]}

    log.info("retrieval_models.download_finished", **{k: v for k, v in result.items() if v})
    return {"outcome": "downloaded", **result}
