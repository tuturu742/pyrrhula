"""Preview jobs: start a container serving a build, stop it, reap expired ones.

The API never touches an engine (it has no socket, no cluster credentials, no boto
session), so every preview action arrives here as a job -- the same split the exec-env
kill endpoint already uses.

These handlers **start and return**. A preview lives for hours; blocking the single
worker loop on one would stall every other job in the deployment.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import quote

import structlog

from core.config import get_settings
from core.ports.preview import PREVIEW_PORT, PreviewUnavailableError
from core.previews.service import (
    build_serve_command,
    due_for_reaping,
    get_preview,
    mark_failed,
    mark_running,
    preview_share_url,
    set_status,
)
from core.repos.service import mint_artifact_read_token
from worker.blob_store_factory import get_blob_store
from worker.preview_factory import get_preview_provider

log = structlog.get_logger()


def _artifact_url(
    store_key: str, artifact_name: str, engine_key: str | None, git_ref: str = ""
) -> str:
    """The in-network address the preview container fetches from -- git_http_base, not
    public_base_url: this hop never leaves the container network.

    The engine's own ``git_http_base`` wins when declared, exactly as the delegation
    work script resolves it. This matters: a preview runs in a *different namespace*
    from the api (pyrrhula-envs vs pyrrhula), where the short Service name does not
    resolve -- the engine declaration is what carries the FQDN."""
    from core.exec_engines import engine_by_key

    engine = engine_by_key(engine_key) or {}
    base = str(engine.get("git_http_base") or get_settings().git_http_base).rstrip("/")
    url = f"{base}/git/{store_key}/artifact?name={artifact_name}"
    # The ref names which branch's build to fetch. Omitted when the preview predates
    # ref-keyed artifacts, where the download path still resolves the old blob key.
    return f"{url}&ref={quote(git_ref, safe='')}" if git_ref else url


async def handle_start_preview(payload: dict[str, Any]) -> dict[str, Any]:
    """Provision the container for an already-claimed ``starting`` row, then post the
    link into the session so the humans in the conversation can open it."""
    tenant_id = uuid.UUID(payload["tenant_id"])
    preview_id = uuid.UUID(payload["preview_id"])
    store_key = str(payload["store_key"])
    share_token = str(payload["share_token"])

    row = await get_preview(tenant_id, preview_id)
    if row is None:
        return {"preview_id": str(preview_id), "outcome": "not_found"}

    # A preview serves an artifact, and the artifact is produced by a delegation whose
    # tests passed -- there is no button that builds a repository on its own. Started
    # against a branch that has never had a green build, the container came up, asked for
    # the artifact, got a 404 and died, and what the operator saw was a preview that
    # failed for no stated reason. The sample most likely to be previewed first is the one
    # whose trunk is red on purpose, so this is the common case, not the edge.
    if row.artifact_name:
        from core.repos.service import artifact_blob_key

        key = artifact_blob_key(store_key, row.artifact_name, row.git_ref or "")
        legacy = artifact_blob_key(store_key, row.artifact_name, "")
        store = get_blob_store()
        if not await store.exists(key) and not await store.exists(legacy):
            reason = (
                f"no {row.artifact_name} has been built for {row.git_ref or 'this branch'}. "
                "An artifact is produced by a delegation on the branch: the agent's "
                "container runs the test command, then the build command, and uploads the "
                "artifact only when the build exits clean. Get a green build on this "
                "branch first, then start the preview."
            )
            await mark_failed(tenant_id, preview_id, reason)
            log.info("preview.no_artifact", preview_id=str(preview_id), ref=row.git_ref)
            return {"preview_id": str(preview_id), "outcome": "failed", "error": reason}

    provider = get_preview_provider(row.engine_key)
    # The recipe the API resolved (repo overrides over pyrrhula-preview.json over the
    # static default). Absent for a job enqueued before this existed, which is exactly
    # the old behaviour.
    port = int(payload.get("port") or PREVIEW_PORT)
    serve_cmd = str(payload.get("serve_cmd") or "")
    command = build_serve_command(port=port, serve_cmd=serve_cmd)
    env = {
        "PYR_ARTIFACT_URL": _artifact_url(
            store_key, row.artifact_name, row.engine_key, row.git_ref
        ),
        # Read-only, one artifact, expires with the preview (plus slack for a restart).
        "PYR_ARTIFACT_TOKEN": mint_artifact_read_token(
            store_key,
            row.artifact_name,
            git_ref=row.git_ref,
            ttl_seconds=get_settings().preview_max_ttl_seconds + 600,
        ),
    }
    # Recipe env is applied UNDER the platform's: the artifact URL and token decide which
    # build this container can read, so a recipe must not be able to reaim them.
    env = {**{str(k): str(v) for k, v in (payload.get("env") or {}).items()}, **env}

    try:
        handle = await provider.start(
            row.name,
            row.image,
            command,
            env=env,
            port=port,
            ttl_seconds=int(payload.get("ttl_seconds") or 0) or None,
        )
    except PreviewUnavailableError as exc:
        await mark_failed(tenant_id, preview_id, str(exc))
        log.warning("preview.start_failed", preview_id=str(preview_id), error=str(exc))
        return {"preview_id": str(preview_id), "outcome": "failed", "error": str(exc)}

    await mark_running(tenant_id, preview_id, ref=handle.ref, internal_url=handle.internal_url)
    url = preview_share_url(share_token)
    if row.session_id is not None:
        await _announce(
            tenant_id,
            row.session_id,
            url,
            row.artifact_name,
            preview_id=preview_id,
            repo_id=row.repo_id,
        )
    log.info("preview.running", preview_id=str(preview_id), name=row.name)
    return {"preview_id": str(preview_id), "outcome": "running", "url": url}


async def _announce(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    url: str,
    artifact_name: str,
    *,
    preview_id: uuid.UUID,
    repo_id: uuid.UUID | None,
) -> None:
    """Put the link in the transcript. Best-effort: a preview that runs but whose note
    fails to post is still a working preview, and the Repos page shows it either way.

    The ids travel with the link because the link itself goes stale: a transcript is
    permanent and a share token is not, so the card that shows it needs to be able to ask
    for a fresh one -- and to redeploy the repo once the container is gone."""
    from core.sessions.notes import post_system_event

    try:
        await post_system_event(
            tenant_id,
            session_id,
            "preview_ready",
            {
                "url": url,
                "artifact_name": artifact_name,
                "preview_id": str(preview_id),
                "repo_id": str(repo_id) if repo_id else "",
            },
        )
    except Exception as exc:  # noqa: BLE001 -- never fail a live preview over a note
        log.warning("preview.announce_failed", session_id=str(session_id), error=str(exc))


async def handle_stop_preview(payload: dict[str, Any]) -> dict[str, Any]:
    """A human stopped it, or the reaper did. Idempotent: an already-gone container
    still ends in the requested terminal status."""
    tenant_id = uuid.UUID(payload["tenant_id"])
    preview_id = uuid.UUID(payload["preview_id"])
    status = str(payload.get("status") or "stopped")

    row = await get_preview(tenant_id, preview_id)
    if row is None:
        return {"preview_id": str(preview_id), "outcome": "not_found"}
    removed = 0
    try:
        removed = await get_preview_provider(row.engine_key).teardown_matching(row.name)
    except PreviewUnavailableError as exc:
        log.warning("preview.teardown_failed", preview_id=str(preview_id), error=str(exc))
    await set_status(tenant_id, preview_id, status)
    return {"preview_id": str(preview_id), "name": row.name, "removed": removed}


async def reap_expired_previews() -> int:
    """Stop every preview past its deadline. Called from the worker's idle tick, not as a
    job: there is no scheduler in this deployment, and engine-side deadlines only exist on
    kubernetes."""
    reaped = 0
    for tenant_id, preview_id, name, engine_key in await due_for_reaping():
        try:
            await get_preview_provider(engine_key).teardown_matching(name)
        except PreviewUnavailableError as exc:
            log.warning("preview.reap_teardown_failed", name=name, error=str(exc))
        await set_status(tenant_id, preview_id, "expired")
        reaped += 1
        log.info("preview.expired", preview_id=str(preview_id), name=name)
    return reaped
