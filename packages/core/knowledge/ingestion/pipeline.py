"""Document -> draft entries -> chunks. Runs entirely against
*draft* entries (``version_id IS NULL``) — publishing is a separate, explicit authoring
action (``core.knowledge.authoring.publish_version``), not something ingestion does on a
caller's behalf. Chunks are keyed by the draft entry's stable id, which is what makes
content-hash skip possible: ``upsert_draft_entry`` updates the same row in place across
re-ingestion runs of an unchanged file, so re-deriving identical chunk text reproduces the
identical ``content_hash`` and the upsert is a no-op — "ingesting the same file twice
produces zero new chunks" falls out of that, not a separate special case.
"""

from __future__ import annotations

import hashlib
import pathlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import text

from core.knowledge.authoring import EntryFields, list_draft_entries, upsert_draft_entry
from core.knowledge.ingestion.chunking import CountTokens, chunk_text
from core.knowledge.ingestion.parsers import (
    ParsedEntry,
    parse_markdown,
    parse_pdf,
    parse_plain_text,
)
from core.tenancy.scope import tenant_scope


class UnsupportedFileTypeError(ValueError):
    pass


_PARSERS: dict[str, Callable[[bytes], list[ParsedEntry]]] = {
    ".md": lambda data: parse_markdown(data.decode("utf-8")),
    ".markdown": lambda data: parse_markdown(data.decode("utf-8")),
    ".txt": lambda data: parse_plain_text(data.decode("utf-8")),
    ".pdf": parse_pdf,
}


@dataclass(frozen=True)
class IngestStats:
    entries_created: int
    entries_updated: int
    chunks_created: int
    chunks_updated: int
    chunks_unchanged: int


async def _upsert_chunk(
    tenant_id: uuid.UUID,
    entry_id: uuid.UUID,
    version_id: uuid.UUID | None,
    ordinal: int,
    chunk_text_: str,
    token_count: int,
    class_: str,
    scope_key: str,
    content_hash: str,
) -> str:
    """Returns 'created', 'updated', or 'unchanged'. A hash match is treated as a no-op
    on purpose — it's the mechanism that keeps an unchanged chunk's embedding
    untouched rather than invalidating vectors that don't need to be recomputed."""
    async with tenant_scope(tenant_id) as session:
        existing = (
            await session.execute(
                text(
                    "SELECT id, content_hash FROM knowledge_chunk "
                    "WHERE entry_id = :entry_id AND ordinal = :ordinal"
                ),
                {"entry_id": entry_id, "ordinal": ordinal},
            )
        ).first()

        if existing is None:
            await session.execute(
                text(
                    "INSERT INTO knowledge_chunk "
                    "(tenant_id, entry_id, version_id, ordinal, text, token_count, class, "
                    " scope_key, content_hash) "
                    "VALUES (:tenant_id, :entry_id, :version_id, :ordinal, :text, "
                    " :token_count, :class_, :scope_key, :content_hash)"
                ),
                {
                    "tenant_id": tenant_id,
                    "entry_id": entry_id,
                    "version_id": version_id,
                    "ordinal": ordinal,
                    "text": chunk_text_,
                    "token_count": token_count,
                    "class_": class_,
                    "scope_key": scope_key,
                    "content_hash": content_hash,
                },
            )
            return "created"

        if existing.content_hash == content_hash:
            return "unchanged"

        # Content changed: text/hash update, and the (now stale) embedding is cleared —
        # the embed job re-embeds any chunk with a NULL embedding.
        await session.execute(
            text(
                "UPDATE knowledge_chunk SET text = :text, token_count = :token_count, "
                "class = :class_, scope_key = :scope_key, content_hash = :content_hash, "
                "embedding = NULL WHERE id = :id"
            ),
            {
                "text": chunk_text_,
                "token_count": token_count,
                "class_": class_,
                "scope_key": scope_key,
                "content_hash": content_hash,
                "id": existing.id,
            },
        )
        return "updated"


async def _delete_stale_chunks(
    tenant_id: uuid.UUID, entry_id: uuid.UUID, keep_below_ordinal: int
) -> None:
    """The document got shorter on re-ingestion — drop chunk rows past the new count so
    they don't linger as retrievable-but-orphaned content."""
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text("DELETE FROM knowledge_chunk WHERE entry_id = :entry_id AND ordinal >= :ordinal"),
            {"entry_id": entry_id, "ordinal": keep_below_ordinal},
        )


async def ingest_document(
    tenant_id: uuid.UUID,
    knowledge_source_id: uuid.UUID,
    *,
    filename: str,
    data: bytes,
    class_: str,
    scope_key: str,
    count_tokens: CountTokens,
) -> IngestStats:
    suffix = pathlib.PurePosixPath(filename).suffix.lower()
    parser = _PARSERS.get(suffix)
    if parser is None:
        raise UnsupportedFileTypeError(
            f"unsupported file type {suffix!r} (expected one of {sorted(_PARSERS)})"
        )
    parsed_entries = parser(data)

    existing_keys = {e.entry_key for e in await list_draft_entries(tenant_id, knowledge_source_id)}

    entries_created = 0
    entries_updated = 0
    chunks_created = 0
    chunks_updated = 0
    chunks_unchanged = 0

    for parsed in parsed_entries:
        if parsed.entry_key in existing_keys:
            entries_updated += 1
        else:
            entries_created += 1

        entry = await upsert_draft_entry(
            tenant_id,
            knowledge_source_id,
            parsed.entry_key,
            EntryFields(
                title=parsed.title, body_md=parsed.body_md, class_=class_, scope_key=scope_key
            ),
        )

        chunks = chunk_text(parsed.body_md, count_tokens)
        for chunk in chunks:
            content_hash = hashlib.sha256(chunk.text.encode()).hexdigest()
            outcome = await _upsert_chunk(
                tenant_id,
                entry.id,
                entry.version_id,
                chunk.ordinal,
                chunk.text,
                chunk.token_count,
                class_,
                scope_key,
                content_hash,
            )
            if outcome == "created":
                chunks_created += 1
            elif outcome == "updated":
                chunks_updated += 1
            else:
                chunks_unchanged += 1

        await _delete_stale_chunks(tenant_id, entry.id, len(chunks))

    return IngestStats(
        entries_created=entries_created,
        entries_updated=entries_updated,
        chunks_created=chunks_created,
        chunks_updated=chunks_updated,
        chunks_unchanged=chunks_unchanged,
    )
