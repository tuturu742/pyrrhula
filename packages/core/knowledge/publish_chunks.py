"""Published entries -> retrieval-ready chunks.

Retrieval reads ``knowledge_chunk``, never ``knowledge_entry``. Publishing a version is
therefore only half of making something retrievable, and the half that is easy to forget:
the entries are visible in the UI, the source looks populated, and the content is silently
absent from every assembled context. ``repo_overview`` shipped in exactly that state -- its
module docstring claimed the overview and graph reached agents "through the normal
retrieval path" while nothing ever chunked them.

Each ingestion path used to carry its own copy of this INSERT. They are collected here so
that "published" and "chunked" stay one step, and so the size bound that keeps the embedding
step from OOM-killing the worker is applied in one place rather than remembered in four.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import text

from core.knowledge.ingestion.chunking import CountTokens, chunk_text
from core.tenancy.scope import tenant_scope


def estimate_tokens(value: str) -> int:
    """``chunk_text`` takes an injected counter because tokenizer choice is an adapter
    concern and ``core`` has no model provider to borrow one from. Machine-shaped content
    -- source code, JSON -- is punctuation-dense, where whitespace-splitting *under*-counts
    badly, so estimate conservatively: the larger of the word count and one token per four
    characters. Over-estimating costs a smaller chunk; under-estimating costs the bound.
    """
    return max(len(value.split()), len(value) // 4)


async def chunk_published_entries(
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    version_id: uuid.UUID,
    *,
    count_tokens: CountTokens = estimate_tokens,
) -> int:
    """Chunks every published entry of ``version_id``. Returns the number of chunks written.

    Re-chunking a version replaces its chunks rather than adding to them, so a caller that
    runs twice converges instead of duplicating. A freshly published version has no chunks
    yet, so the delete is a no-op on the normal path.
    """
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text(
                "DELETE FROM knowledge_chunk c USING knowledge_entry e "
                "WHERE c.entry_id = e.id AND e.knowledge_source_id = :s AND e.version_id = :v"
            ),
            {"s": source_id, "v": version_id},
        )

        entries = (
            await session.execute(
                text(
                    "SELECT id, body_md, class, scope_key FROM knowledge_entry "
                    "WHERE knowledge_source_id = :s AND version_id = :v "
                    "  AND length(trim(body_md)) > 0"
                ),
                {"s": source_id, "v": version_id},
            )
        ).all()

        written = 0
        for entry_id, body_md, class_, scope_key in entries:
            for chunk in chunk_text(body_md, count_tokens):
                await session.execute(
                    text(
                        "INSERT INTO knowledge_chunk "
                        "(tenant_id, entry_id, version_id, ordinal, text, token_count, "
                        " class, scope_key, embedding, content_hash) "
                        "VALUES (:tenant_id, :entry_id, :version_id, :ordinal, :text, "
                        " :token_count, :class_, :scope_key, NULL, :content_hash)"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "entry_id": entry_id,
                        "version_id": version_id,
                        "ordinal": chunk.ordinal,
                        "text": chunk.text,
                        "token_count": chunk.token_count,
                        "class_": class_,
                        "scope_key": scope_key,
                        # sha256 matches A1.2's pipeline. The embedding cache is keyed on
                        # content_hash, so agreeing on the digest is what lets text shared
                        # between two ingestion paths be embedded once, not once per path.
                        "content_hash": hashlib.sha256(chunk.text.encode()).hexdigest(),
                    },
                )
                written += 1
        return written
