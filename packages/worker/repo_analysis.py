"""Repo knowledge-graph analysis (`analyze_workspace_repos`): read the hosted store's
trees, ingest each repo as knowledge, then have the workspace assistant's model write
per-repo summaries, a cross-repo overview ("what do these repos collectively achieve"),
and a structured graph -- all persisted as ordinary published knowledge entries
(`core.knowledge.repo_overview`), so every agent's assembled context benefits, and the
UI's graph page is just a second rendering of the same rows.

Idempotent on (workspace, the analyzed repos' HEAD SHAs): re-running with unchanged repos
is a replay, not a re-generation. Model failures degrade to listing-derived scaffold
summaries and a repos-only graph (logged `repo_analysis.fallback`) rather than failing
the job -- the same graceful-degradation stance as codegen.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

import structlog
from pydantic import BaseModel

from adapters.mcp.git_store import GitStore, GitStoreError, default_git_root
from core.actions.idempotency import clear_failed_operation, idempotent
from core.agents.assistant import ensure_workspace_assistant
from core.agents.authoring import get_agent, resolve_connection_api_key
from core.agents.models import Agent
from core.audit.models import UsageRecordRow
from core.knowledge.repo_ingestion import RepoFile, ingest_repo_snapshot
from core.knowledge.repo_overview import GraphNode, RepoGraph, save_repo_overview
from core.ports.model_provider import GenerationRequest, ModelProvider
from core.repos.service import get_repo, store_key
from core.tenancy.egress import load_egress_policy
from core.tenancy.scope import tenant_scope
from worker.encryptor_factory import get_encryptor
from worker.job_queue_factory import get_job_queue
from worker.model_provider_factory import get_model_provider

log = structlog.get_logger()

_PURPOSE = "report"
_MAX_TREE_FILES = 150
_MAX_FILE_BYTES = 16_000
# How much file content one summary prompt may carry -- local-model context is finite.
_MAX_PROMPT_CHARS = 24_000

_SUMMARY_SYSTEM = (
    "You analyze a software repository from its file listing and key file contents. "
    "Write a concise markdown summary (4-10 sentences): what the project is, its main "
    "components, and how it fits with sibling projects if that is apparent. Return only "
    "the summary text."
)
_OVERVIEW_SYSTEM = (
    "You are given one summary per repository in a multi-repo project. Write a concise "
    "markdown overview of what these repositories collectively try to achieve: the "
    "shared goal, each repo's role, and how they relate. Return only the overview text."
)
_GRAPH_SYSTEM = (
    "You are given one summary per repository in a multi-repo project. Produce a small "
    "knowledge graph: one node per repository (kind 'repo', id = the repo key) plus at "
    "most five shared-concept nodes (kind 'concept'), and edges for the real "
    "relationships between them (depends-on, implements, documents, shares-data...). "
    "Keep every node id short and unique."
)


class _Prose(BaseModel):
    text: str


def _files_digest(shas: dict[str, str]) -> str:
    canonical = "|".join(f"{k}@{v}" for k, v in sorted(shas.items()))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _scaffold_summary(repo_key: str, paths: list[str]) -> str:
    listing = "\n".join(f"- `{p}`" for p in paths[:40])
    return (
        f"Repository `{repo_key}` ({len(paths)} files at analysis time). "
        f"Automatic summary unavailable; file listing:\n{listing}"
    )


async def _call_prose(
    provider: ModelProvider,
    profile: Agent,
    system: str,
    user: str,
    *,
    tenant_id: uuid.UUID,
    api_key: str | None,
    metering: list[tuple[int, int, int]],
    max_tokens: int = 700,
) -> str:
    model_string = f"{profile.provider}/{profile.model}"
    req = GenerationRequest(
        egress_policy=await load_egress_policy(tenant_id),
        model=model_string,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user[:_MAX_PROMPT_CHARS]},
        ],
        purpose=_PURPOSE,
        max_tokens=max_tokens,
        api_base=profile.api_base,
        params=dict(profile.params or {}),
        api_key=api_key,
    )
    start = time.monotonic()
    result = await provider.generate_structured(req, _Prose)
    metering.append(
        (
            sum(
                provider.count_tokens(str(m.get("content") or ""), model_string)
                for m in req.messages
            ),
            provider.count_tokens(result.text, model_string),
            int((time.monotonic() - start) * 1000),
        )
    )
    return result.text.strip()


async def _call_graph(
    provider: ModelProvider,
    profile: Agent,
    user: str,
    *,
    tenant_id: uuid.UUID,
    api_key: str | None,
    metering: list[tuple[int, int, int]],
) -> RepoGraph:
    model_string = f"{profile.provider}/{profile.model}"
    req = GenerationRequest(
        egress_policy=await load_egress_policy(tenant_id),
        model=model_string,
        messages=[
            {"role": "system", "content": _GRAPH_SYSTEM},
            {"role": "user", "content": user[:_MAX_PROMPT_CHARS]},
        ],
        purpose=_PURPOSE,
        max_tokens=1200,
        api_base=profile.api_base,
        params=dict(profile.params or {}),
        api_key=api_key,
    )
    start = time.monotonic()
    result = await provider.generate_structured(req, RepoGraph)
    metering.append(
        (
            sum(
                provider.count_tokens(str(m.get("content") or ""), model_string)
                for m in req.messages
            ),
            provider.count_tokens(result.model_dump_json(), model_string),
            int((time.monotonic() - start) * 1000),
        )
    )
    return result


async def _meter(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    profile: Agent,
    rows: list[tuple[int, int, int]],
) -> None:
    if not rows:
        return
    async with tenant_scope(tenant_id) as session:
        for prompt_tokens, completion_tokens, latency_ms in rows:
            session.add(
                UsageRecordRow(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    agent_id=profile.id,
                    provider=profile.provider,
                    model=profile.model,
                    purpose=_PURPOSE,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=latency_ms,
                )
            )


def _analysis_job_key(**kwargs: Any) -> str:
    return f"analyze_workspace_repos:{kwargs['workspace_id']}:{_files_digest(kwargs['head_shas'])}"


@idempotent(key_fn=_analysis_job_key)
async def _run_analysis(
    *,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    repo_keys: list[str],
    head_shas: dict[str, str],
    trees: dict[str, dict[str, str]],
) -> dict[str, Any]:
    persona = await ensure_workspace_assistant(tenant_id, workspace_id)
    profile = await get_agent(tenant_id, persona.agent_id)
    if profile is None:
        raise ValueError("the workspace assistant's model profile is missing")
    provider = get_model_provider(profile.provider)
    api_key = await resolve_connection_api_key(
        tenant_id, profile.credential_ref, encryptor=get_encryptor()
    )
    metering: list[tuple[int, int, int]] = []
    fallbacks: list[str] = []

    # 1. Each repo becomes knowledge through the standard snapshot pipeline (no-op per
    # unchanged SHA), then gets its chunks embedded.
    ingested_sources: list[str] = []
    for repo_key in repo_keys:
        report = await ingest_repo_snapshot(
            tenant_id,
            workspace_id,
            repo_ref=repo_key,
            commit_sha=head_shas[repo_key],
            files=[RepoFile(path=p, content=c) for p, c in sorted(trees[repo_key].items())],
        )
        ingested_sources.append(str(report.knowledge_source_id))
        if not report.unchanged:
            await get_job_queue().enqueue(
                tenant_id,
                "embed_chunks",
                {
                    "tenant_id": str(tenant_id),
                    "knowledge_source_id": str(report.knowledge_source_id),
                },
            )

    # 2. Per-repo summaries.
    summaries: dict[str, str] = {}
    for repo_key in repo_keys:
        tree = trees[repo_key]
        listing = "\n".join(sorted(tree))
        # READMEs first, then whatever else fits the prompt budget.
        ordered = sorted(
            ((p, c) for p, c in tree.items() if c),
            key=lambda pc: (not pc[0].upper().startswith("README"), pc[0]),
        )
        excerpt_parts: list[str] = []
        budget = _MAX_PROMPT_CHARS - len(listing) - 500
        for path, content in ordered:
            block = f"=== {path} ===\n{content}"
            if len(block) > budget:
                continue
            excerpt_parts.append(block)
            budget -= len(block)
        user = f"Repository: {repo_key}\n\nFiles:\n{listing}\n\nKey contents:\n" + "\n\n".join(
            excerpt_parts
        )
        try:
            summaries[repo_key] = await _call_prose(
                provider,
                profile,
                _SUMMARY_SYSTEM,
                user,
                tenant_id=tenant_id,
                api_key=api_key,
                metering=metering,
            )
        except Exception as exc:  # noqa: BLE001 -- degrade, don't fail the job
            log.warning("repo_analysis.fallback", repo=repo_key, step="summary", error=str(exc))
            fallbacks.append(repo_key)
            summaries[repo_key] = _scaffold_summary(repo_key, sorted(tree))

    # 3. Cross-repo overview + graph.
    combined = "\n\n".join(f"## {k}\n{v}" for k, v in summaries.items())
    try:
        overview = await _call_prose(
            provider,
            profile,
            _OVERVIEW_SYSTEM,
            combined,
            tenant_id=tenant_id,
            api_key=api_key,
            metering=metering,
            max_tokens=900,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("repo_analysis.fallback", step="overview", error=str(exc))
        fallbacks.append("overview")
        overview = "## Repo overview\n\n" + combined
    try:
        graph = await _call_graph(
            provider,
            profile,
            combined,
            tenant_id=tenant_id,
            api_key=api_key,
            metering=metering,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("repo_analysis.fallback", step="graph", error=str(exc))
        fallbacks.append("graph")
        graph = RepoGraph(
            nodes=[
                GraphNode(id=k, label=k, kind="repo", summary=summaries[k][:300]) for k in repo_keys
            ],
            edges=[],
        )
    # Whatever the model said, the analyzed repos themselves are always nodes.
    present = {n.id for n in graph.nodes}
    for repo_key in repo_keys:
        if repo_key not in present:
            graph.nodes.append(
                GraphNode(
                    id=repo_key, label=repo_key, kind="repo", summary=summaries[repo_key][:300]
                )
            )

    await _meter(tenant_id, workspace_id, profile, metering)

    source_id, version_id = await save_repo_overview(
        tenant_id,
        workspace_id,
        overview_md=overview,
        graph=graph,
        repo_summaries=summaries,
        change_note=f"repo analysis {_files_digest(head_shas)}",
    )
    await get_job_queue().enqueue(
        tenant_id,
        "embed_chunks",
        {"tenant_id": str(tenant_id), "knowledge_source_id": str(source_id)},
    )

    result = {
        "source_id": str(source_id),
        "version_id": str(version_id),
        "repos": {k: head_shas[k] for k in repo_keys},
        "node_count": len(graph.nodes),
        "edge_count": len(graph.edges),
        "ingested_sources": ingested_sources,
        "fallbacks": fallbacks,
    }

    # This job is idempotent on content -- workspace plus HEAD SHAs -- so a result is
    # cached until the repository itself changes. That is right for a good analysis and
    # a trap for a degraded one: when every prose step fell back (a misconfigured
    # assistant model, a provider outage), the cache pinned a graph of bare file listings
    # in place, and fixing the cause changed nothing because the re-run never ran. A
    # bench then plans against a file listing and cannot tell.
    #
    # An analysis that produced no prose at all is not a result worth keeping. Raising
    # here leaves it retryable rather than recorded as done.
    if fallbacks and not any(step not in fallbacks for step in ("overview", "graph")):
        raise RuntimeError(
            "repo analysis produced no model-written summaries "
            f"(every step fell back: {sorted(set(fallbacks))}). "
            "Check the workspace assistant has a model profile with a provider and a "
            "model set; the graph is a file listing without it."
        )
    return result


async def handle_analyze_workspace_repos(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolves registry rows and reads store trees *outside* the idempotent core, so the
    idempotency key can be the content identity (workspace + HEAD SHAs) rather than the
    request shape."""
    tenant_id = uuid.UUID(payload["tenant_id"])
    workspace_id = uuid.UUID(payload["workspace_id"])
    store = GitStore(default_git_root())

    repo_keys: list[str] = []
    head_shas: dict[str, str] = {}
    trees: dict[str, dict[str, str]] = {}
    for raw_id in payload["repo_ids"]:
        repo = await get_repo(tenant_id, uuid.UUID(raw_id))
        if repo is None:
            raise ValueError(f"no repo {raw_id} in this tenant")
        skey = store_key(tenant_id, repo.key)
        try:
            # The repository's own branch. Assuming "main" silently skipped every repo
            # whose remote called it something else -- GitStoreError is caught below and
            # only logged, so two of three repositories could vanish from an analysis
            # with nothing on screen to say so.
            branch = repo.default_branch or "main"
            sha = await store.head_sha(skey, branch)
            tree = await store.read_tree(
                skey,
                ref=branch,
                max_files=_MAX_TREE_FILES,
                max_file_bytes=_MAX_FILE_BYTES,
            )
        except GitStoreError as exc:
            log.warning("repo_analysis.unreadable_repo", repo=repo.key, error=str(exc))
            continue
        repo_keys.append(repo.key)
        head_shas[repo.key] = sha
        trees[repo.key] = tree

    if not repo_keys:
        raise ValueError("none of the requested repos are readable in the hosted store")

    # `@idempotent` records a failure permanently, so the degraded analysis that
    # `_run_analysis` refuses to keep (every prose step fell back) would otherwise block
    # every later "Analyze repos" for the same content -- fixing the assistant's model
    # changed nothing, because the retry never ran. Nothing before the summaries has a
    # side effect that a re-run could double (ingestion is a no-op per unchanged SHA), so
    # a recorded failure for this key is cleared before each attempt.
    await clear_failed_operation(
        tenant_id, _analysis_job_key(workspace_id=workspace_id, head_shas=head_shas)
    )
    return await _run_analysis(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        repo_keys=repo_keys,
        head_shas=head_shas,
        trees=trees,
    )
