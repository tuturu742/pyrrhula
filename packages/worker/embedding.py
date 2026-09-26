"""Embedding job handlers  — the worker-side wiring ``core.knowledge.embedding``
needs but can't import itself: which ``EmbeddingProvider`` adapter
(``worker.embedding_provider_factory``), and the tenant's egress policy
(``Tenant.settings["egress_policy"]``). Registered in ``worker.main``'s job dispatch
table under kinds ``"embed_chunks"`` (chained after ``knowledge_ingest``) and
``"reembed_stale"`` (a tenant-wide sweep, triggered when the configured embedding model
changes).
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any, cast

from core.knowledge.embedding import embed_chunks
from core.tenancy.models import Tenant
from core.tenancy.scope import unscoped_session
from worker.embedding_provider_factory import get_embedding_provider


async def _tenant_egress_policy(tenant_id: uuid.UUID) -> dict[str, list[str]]:
    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return {}
    return cast("dict[str, list[str]]", tenant.settings.get("egress_policy", {}))


async def handle_embed_chunks(payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = uuid.UUID(payload["tenant_id"])
    knowledge_source_id = uuid.UUID(payload["knowledge_source_id"])
    provider = get_embedding_provider()
    egress_policy = await _tenant_egress_policy(tenant_id)

    stats = await embed_chunks(
        tenant_id,
        provider,
        knowledge_source_id=knowledge_source_id,
        egress_policy=egress_policy,
    )
    return asdict(stats)


async def handle_reembed_stale(payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = uuid.UUID(payload["tenant_id"])
    provider = get_embedding_provider()
    egress_policy = await _tenant_egress_policy(tenant_id)

    stats = await embed_chunks(
        tenant_id, provider, knowledge_source_id=None, egress_policy=egress_policy
    )
    return asdict(stats)
