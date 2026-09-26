"""Which knowledge *versions* a workspace actually reads (the pin-vs-follow, resolved).

``knowledge_chunk`` keeps every published version's chunks side by side: publishing a new
version inserts new rows and leaves the old ones exactly where they were (chunking a
version replaces only *that* version's chunks -- see ``publish_chunks``). That is correct
storage and a trap for retrieval, because dense and sparse search read
``knowledge_chunk`` directly. Without a version predicate the superseded text is still
embedded, still indexed, and still ranks -- so an entry that was corrected goes on being
citable in its original wording, and a turn can quote a paragraph the workspace has
already replaced. The failure is silent and reads as a model error rather than a
retrieval one: the agent cites `k9`, `k9` says what it says, and nothing in the trace
says that text is two versions old.

The resolution rule is the same one the activation path has always applied
(``_activate_for_workspace``): per enabled attachment, ``version_pin`` if set, otherwise
the source's ``current_version_id``. An empty result means the workspace has nothing
attached, which is not an error -- it is a workspace with no knowledge, and the caller
should retrieve nothing rather than everything.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from core.tenancy.scope import tenant_scope


async def effective_version_ids(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """The published version of every source this workspace has enabled."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT COALESCE(a.version_pin, s.current_version_id) AS version_id "
                    "FROM workspace_knowledge_attachment a "
                    "JOIN knowledge_source s ON s.id = a.knowledge_source_id "
                    "WHERE a.workspace_id = :workspace_id AND a.enabled "
                    "  AND COALESCE(a.version_pin, s.current_version_id) IS NOT NULL"
                ),
                {"workspace_id": workspace_id},
            )
        ).all()
    return frozenset(row[0] for row in rows)
