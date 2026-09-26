"""the worker-side embedding job handlers, including the D14 egress check reading
a real tenant's ``settings.egress_policy`` -- exercised against a live Postgres.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import update

from core.knowledge.authoring import create_source
from core.knowledge.ingestion.pipeline import ingest_document
from core.ports.model_provider import EgressDeniedError
from core.tenancy.models import Tenant
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant
from worker import embedding_provider_factory
from worker.embedding import handle_embed_chunks, handle_reembed_stale


def _word_count(text_: str) -> int:
    return len(text_.split())


async def _setup_with_ingested_chunk(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    await ingest_document(
        tenant_id,
        source.id,
        filename="rules.md",
        data=b"# Grappling\nRoll 1d20+STR.\n",
        class_="rules",
        scope_key="workspace_public",
        count_tokens=_word_count,
    )
    return tenant_id, source.id


@pytest.fixture(autouse=True)
def _use_stub_embedding_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the worker's factory to the fast, offline stub model for these tests --
    they exercise job-handler wiring and egress policy, not a real model. Dimension stays
    at the default 1024: knowledge_chunk.embedding is a fixed vector(1024) column,
    so the stub must match it here (unlike the adapter's own isolated unit tests, which
    never touch that table)."""
    monkeypatch.setattr(embedding_provider_factory.get_settings(), "embedding_model", "local/stub")


async def test_handle_embed_chunks_end_to_end(db_available: None) -> None:
    tenant_id, source_id = await _setup_with_ingested_chunk("worker-emb")

    result = await handle_embed_chunks(
        {"tenant_id": str(tenant_id), "knowledge_source_id": str(source_id)}
    )
    assert result["embedded"] == 1


async def test_handle_reembed_stale_sweeps_whole_tenant(db_available: None) -> None:
    tenant_id, _source_id = await _setup_with_ingested_chunk("worker-reembed")
    await handle_embed_chunks({"tenant_id": str(tenant_id), "knowledge_source_id": str(_source_id)})

    result = await handle_reembed_stale({"tenant_id": str(tenant_id)})
    # Already embedded under the current (stub) model tag -- nothing left to do.
    assert result["embedded"] == 0
    assert result["reused_from_cache"] == 0


async def test_embed_chunks_respects_tenant_egress_policy_blocking_cloud(
    db_available: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, source_id = await _setup_with_ingested_chunk("worker-egress")
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(settings={"egress_policy": {"embed": ["local"]}})
        )

    class _CloudProvider:
        model_name = "openai/text-embedding-3-small"
        dimension = 8

        async def embed(self, req):  # noqa: ANN001, ANN201
            from core.ports.model_provider import check_egress

            check_egress(req.purpose, req.model, req.egress_policy)
            raise AssertionError("should never reach here -- egress must deny first")

    # worker.embedding does `from worker.embedding_provider_factory import
    # get_embedding_provider`, binding the name into its own module namespace --
    # patching that name where it's actually looked up, not the origin module.
    import worker.embedding as embedding_module

    monkeypatch.setattr(
        embedding_module, "get_embedding_provider", lambda *a, **kw: _CloudProvider()
    )

    with pytest.raises(EgressDeniedError):
        await handle_embed_chunks(
            {"tenant_id": str(tenant_id), "knowledge_source_id": str(source_id)}
        )
