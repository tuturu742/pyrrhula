"""The cross-repo overview -- "what do these repos collectively achieve" as knowledge.

The analysis worker writes three kinds of entry into one workspace-attached knowledge
source (key ``repo-overview-<workspace8>``): ``overview`` (markdown, the collective
picture), ``repo-<key>`` (one summary per analyzed repo), and ``graph`` (structured JSON
-- nodes and edges -- that the UI renders with React Flow). Because they are ordinary
published knowledge entries, every agent's assembled context benefits from them through
the normal retrieval path; the graph page is just a second rendering of the same rows.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel
from sqlalchemy import select

from core.knowledge.authoring import (
    EntryFields,
    attach_source_to_workspace,
    create_source,
    list_version_entries,
    list_versions,
    publish_version,
    upsert_draft_entry,
)
from core.knowledge.models import KnowledgeEntry, KnowledgeSource
from core.tenancy.scope import tenant_scope

_SCOPE_KEY = "workspace_public"


class GraphNode(BaseModel):
    id: str
    label: str
    kind: str = "repo"  # repo | module | service | concept
    summary: str = ""


class GraphEdge(BaseModel):
    source: str
    target: str
    label: str = ""


class RepoGraph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


def overview_source_key(workspace_id: uuid.UUID) -> str:
    return f"repo-overview-{workspace_id.hex[:8]}"


async def _get_or_create_source(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> KnowledgeSource:
    key = overview_source_key(workspace_id)
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(KnowledgeSource).where(
                KnowledgeSource.tenant_id == tenant_id, KnowledgeSource.key == key
            )
        )
        if existing is not None:
            session.expunge(existing)
            return existing
    source = await create_source(tenant_id, key=key, name="Repo overview", class_="lore")
    await attach_source_to_workspace(tenant_id, workspace_id, source.id, _SCOPE_KEY)
    return source


async def save_repo_overview(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    overview_md: str,
    graph: RepoGraph,
    repo_summaries: dict[str, str],
    change_note: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Upserts the overview/summary/graph entries and publishes a new version. Returns
    (source_id, version_id). The graph entry's class is ``misc`` -- it is machine-shaped
    JSON, useful to the UI and harmless-but-uninteresting to retrieval."""
    source = await _get_or_create_source(tenant_id, workspace_id)
    await upsert_draft_entry(
        tenant_id,
        source.id,
        "overview",
        EntryFields(
            title="Repo overview", body_md=overview_md, class_="lore", scope_key=_SCOPE_KEY
        ),
    )
    for repo_key, summary_md in repo_summaries.items():
        await upsert_draft_entry(
            tenant_id,
            source.id,
            f"repo-{repo_key}",
            EntryFields(
                title=f"Repo: {repo_key}",
                body_md=summary_md,
                class_="lore",
                scope_key=_SCOPE_KEY,
            ),
        )
    await upsert_draft_entry(
        tenant_id,
        source.id,
        "graph",
        EntryFields(
            title="Repo graph",
            body_md=graph.model_dump_json(indent=2),
            class_="misc",
            scope_key=_SCOPE_KEY,
        ),
    )
    # Publishing chunks the version (core.knowledge.authoring.publish_version); before it
    # did, this module chunked here itself, and before THAT it chunked nowhere and its
    # docstring promised retrieval anyway.
    version = await publish_version(tenant_id, source.id, change_note=change_note)
    return source.id, version.id


async def list_published_overview_entries(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> list[KnowledgeEntry]:
    """The latest published version's entries, or [] before any analysis has run."""
    key = overview_source_key(workspace_id)
    async with tenant_scope(tenant_id) as session:
        source = await session.scalar(
            select(KnowledgeSource).where(
                KnowledgeSource.tenant_id == tenant_id, KnowledgeSource.key == key
            )
        )
    if source is None:
        return []
    versions = await list_versions(tenant_id, source.id)
    if not versions:
        return []
    latest = max(versions, key=lambda v: v.created_at)
    return await list_version_entries(tenant_id, latest.id)
