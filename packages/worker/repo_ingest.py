"""Repo-snapshot ingestion job (G4.15) -- registered in `worker.main` under
`"ingest_repo_snapshot"`.

A worker job because a repository is large and the pipeline embeds: the API stores the
tarball as a blob and enqueues, exactly as `worker.ingestion` does for a document upload.

Idempotent on `(workspace, repo_ref, sha)`, which is the same identity
`ingest_repo_snapshot` itself treats as a no-op. Two mechanisms agreeing is fine here --
the inner one is the correctness guarantee (a repeated SHA writes nothing whatever calls
it), the outer one just avoids re-reading a tarball to discover that.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.actions.idempotency import idempotent
from core.knowledge.repo_ingestion import ingest_repo_snapshot, read_tarball
from worker.blob_store_factory import get_blob_store


def _repo_job_key(**kwargs: Any) -> str:
    return (
        f"ingest_repo_snapshot:{kwargs['workspace_id']}:{kwargs['repo_ref']}:{kwargs['commit_sha']}"
    )


@idempotent(key_fn=_repo_job_key)
async def run_repo_ingest_job(
    *,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    repo_ref: str,
    commit_sha: str,
    blob_key: str,
) -> dict[str, Any]:
    data = await get_blob_store().get(blob_key)
    report = await ingest_repo_snapshot(
        tenant_id,
        workspace_id,
        repo_ref=repo_ref,
        commit_sha=commit_sha,
        files=read_tarball(data),
    )
    return {
        "knowledge_source_id": str(report.knowledge_source_id),
        "version_id": str(report.version_id) if report.version_id else None,
        "commit_sha": report.commit_sha,
        "entries": report.entries,
        "unchanged": report.unchanged,
        "by_class": report.by_class,
        "quarantined": [{"entry_key": k, "reason": r} for k, r in report.quarantined],
        "experimental_paths": report.experimental,
    }


async def handle_ingest_repo_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    return await run_repo_ingest_job(
        tenant_id=uuid.UUID(payload["tenant_id"]),
        workspace_id=uuid.UUID(payload["workspace_id"]),
        repo_ref=str(payload["repo_ref"]),
        commit_sha=str(payload["commit_sha"]),
        blob_key=str(payload["blob_key"]),
    )
