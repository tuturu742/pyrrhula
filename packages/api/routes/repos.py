"""Tenant repo registry endpoints.

Registering a repo creates its hosted server-side git repository (and best-effort clones the
optional ``source_url`` -- private https remotes authenticate with an access token sealed via
the Encryptor into a ``provider_credential`` row; the token itself is write-only and never
returned). Reads are open to authenticated members; writes are gated on ``repo:manage``
(tenant, owner + admin). Archive is a soft delete -- the hosted content stays.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from adapters.gitremote.registry import resolve_remote
from adapters.mcp.git_store import GitStore, GitStoreError, default_git_root
from api.authz import require_tenant_permission
from api.encryptor_factory import get_encryptor
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from api.play_headers import PLAY_HEADERS, content_type_for
from core.agents.authoring import store_provider_credential
from core.repos.models import RepoRow
from core.repos.service import (
    GIT_PROVIDERS,
    RUNTIME_CATALOG,
    InvalidRepoError,
    RepoNotFoundError,
    archive_repo,
    create_repo,
    list_repos,
    store_key,
)
from core.tenancy.context import RequestContext

router = APIRouter(
    prefix="/repos",
    tags=["repos"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


class RepoResponse(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    description: str = ""
    source_url: str | None = None
    provider: str | None = None
    has_credential: bool = False
    runtime: str = "debian"
    runtime_image: str | None = None
    has_registry_credential: bool = False
    setup_cmds: list[str] = []
    test_cmd: str | None = None
    build_cmd: str | None = None
    artifact_name: str | None = None
    created_at: datetime
    import_status: str | None = None


def _response(row: RepoRow, *, import_status: str | None = None) -> RepoResponse:
    return RepoResponse(
        id=row.id,
        key=row.key,
        name=row.name,
        description=row.description,
        source_url=row.source_url,
        provider=row.provider,
        has_credential=row.credential_ref is not None,
        runtime=row.runtime,
        runtime_image=row.runtime_image,
        has_registry_credential=row.registry_credential_ref is not None,
        setup_cmds=list(row.setup_cmds or []),
        test_cmd=row.test_cmd,
        build_cmd=row.build_cmd,
        artifact_name=row.artifact_name,
        preview_image=row.preview_image,
        preview_cmd=row.preview_cmd,
        preview_port=row.preview_port,
        preview_env={str(k): str(v) for k, v in (row.preview_env or {}).items()},
        created_at=row.created_at,
        import_status=import_status,
    )


class ExecEngineResponse(BaseModel):
    key: str
    kind: str
    label: str = ""
    current: bool = False


@router.get("/exec-engines")
async def list_exec_engines_endpoint(
    ctx: RequestContext = Depends(get_request_context),
) -> list[ExecEngineResponse]:
    """The engines this deployment declares (where agent environments run) + which one
    this tenant currently uses."""
    from core.exec_engines import declared_engines, get_tenant_engine_key

    current = await get_tenant_engine_key(ctx.tenant_id)
    return [
        ExecEngineResponse(
            key=str(e["key"]),
            kind=str(e["kind"]),
            label=str(e.get("label") or e["key"]),
            current=e["key"] == current,
        )
        for e in declared_engines()
    ]


class SetExecEngineRequest(BaseModel):
    engine: str


@router.put("/exec-engines/current")
async def set_exec_engine_endpoint(
    body: SetExecEngineRequest, ctx: RequestContext = Depends(get_request_context)
) -> list[ExecEngineResponse]:
    from core.exec_engines import UnknownEngineError, set_tenant_engine_key

    await require_tenant_permission(ctx, "repo:manage")
    try:
        await set_tenant_engine_key(ctx.tenant_id, body.engine)
    except UnknownEngineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await list_exec_engines_endpoint(ctx)


class ExecEnvironmentResponse(BaseModel):
    id: uuid.UUID
    name: str
    engine_key: str | None = None
    image: str = ""
    status: str
    session_id: uuid.UUID | None = None
    session_name: str = ""
    repo_id: uuid.UUID | None = None
    spawned_by_persona_id: uuid.UUID | None = None
    spawned_by_label: str = ""
    last_exit_code: int | None = None
    created_at: datetime
    updated_at: datetime


@router.get("/exec-environments")
async def list_exec_environments_endpoint(
    include_finished: bool = False,
    ctx: RequestContext = Depends(get_request_context),
) -> list[ExecEnvironmentResponse]:
    """The tracked exec environments (delegated coding work): which actor spawned
    which one, on which engine, and whether it is still active."""
    from core.exec_envs import list_environments

    rows = await list_environments(ctx.tenant_id, include_finished=include_finished)
    return [ExecEnvironmentResponse(**row) for row in rows]


@router.post("/exec-environments/{environment_id}/kill", status_code=202)
async def kill_exec_environment_endpoint(
    environment_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
) -> ExecEnvironmentResponse:
    """Request a teardown of one environment (stuck/orphaned). The worker holds the
    engine access, so this marks the row and enqueues the actual kill."""
    from api.job_queue_factory import get_job_queue
    from core.exec_envs import get_environment, set_status

    await require_tenant_permission(ctx, "repo:manage")
    row = await get_environment(ctx.tenant_id, environment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such environment")
    await set_status(ctx.tenant_id, environment_id, "kill_requested")
    await get_job_queue().enqueue(
        ctx.tenant_id,
        "kill_exec_environment",
        {"tenant_id": str(ctx.tenant_id), "environment_id": str(environment_id)},
    )
    refreshed = await get_environment(ctx.tenant_id, environment_id)
    assert refreshed is not None
    return ExecEnvironmentResponse(
        id=refreshed.id,
        name=refreshed.name,
        engine_key=refreshed.engine_key,
        image=refreshed.image,
        status=refreshed.status,
        session_id=refreshed.session_id,
        repo_id=refreshed.repo_id,
        spawned_by_persona_id=refreshed.spawned_by_persona_id,
        spawned_by_label=refreshed.spawned_by_label,
        last_exit_code=refreshed.last_exit_code,
        created_at=refreshed.created_at,
        updated_at=refreshed.updated_at,
    )


class RuntimeResponse(BaseModel):
    key: str
    image: str


@router.get("/runtimes")
async def list_runtimes_endpoint() -> list[RuntimeResponse]:
    """The curated exec-runtime catalog (+ the 'custom' sentinel: bring your own image)."""
    entries = [
        RuntimeResponse(key=k, image=str(v["image"])) for k, v in sorted(RUNTIME_CATALOG.items())
    ]
    entries.append(RuntimeResponse(key="custom", image=""))
    return entries


@router.get("")
async def list_repos_endpoint(
    ctx: RequestContext = Depends(get_request_context),
) -> list[RepoResponse]:
    return [_response(r) for r in await list_repos(ctx.tenant_id)]


class CreateRepoRequest(BaseModel):
    key: str
    name: str
    description: str = ""
    source_url: str | None = None
    provider: str | None = None  # github|gitlab|gitea|generic; None = auto-detect
    # Write-only: sealed via the Encryptor into provider_credential; never echoed back.
    access_token: str | None = None
    runtime: str = "debian"
    runtime_image: str | None = None
    # Write-only registry credentials for pulling a private runtime_image.
    registry_username: str | None = None
    registry_token: str | None = None
    setup_cmds: list[str] = []
    test_cmd: str | None = None
    build_cmd: str | None = None
    artifact_name: str | None = None
    # Preview recipe overrides; unset means the repo's pyrrhula-preview.json decides, and
    # failing that the platform's static-site server.
    preview_image: str | None = None
    preview_cmd: str | None = None
    preview_port: int | None = None
    preview_env: dict[str, str] = {}


async def _seal_registry_credentials(
    tenant_id: uuid.UUID, username: str | None, token: str | None
) -> uuid.UUID | None:
    """Registry credentials (for private runtime images) sealed as one encrypted JSON
    payload -- same store and write-only discipline as repo access tokens."""
    if not token:
        return None
    import json as _json

    payload = _json.dumps({"username": username or "", "password": token})
    return await store_provider_credential(tenant_id, payload, encryptor=get_encryptor())


@router.post("", status_code=201)
async def create_repo_endpoint(
    body: CreateRepoRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> RepoResponse:
    await require_tenant_permission(ctx, "repo:manage")

    credential_ref: uuid.UUID | None = None
    if body.access_token:
        credential_ref = await store_provider_credential(
            ctx.tenant_id, body.access_token, encryptor=get_encryptor()
        )
    registry_credential_ref = await _seal_registry_credentials(
        ctx.tenant_id, body.registry_username, body.registry_token
    )

    try:
        row = await create_repo(
            ctx.tenant_id,
            body.key,
            body.name,
            description=body.description,
            source_url=body.source_url,
            provider=body.provider,
            credential_ref=credential_ref,
            runtime=body.runtime,
            runtime_image=body.runtime_image,
            registry_credential_ref=registry_credential_ref,
            setup_cmds=body.setup_cmds,
            test_cmd=body.test_cmd,
            build_cmd=body.build_cmd,
            artifact_name=body.artifact_name,
            created_by=ctx.principal_id,
        )
    except InvalidRepoError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        if "uq_repo_tenant_key" in str(exc):
            raise HTTPException(
                status_code=409, detail=f"repo {body.key!r} already exists"
            ) from exc
        raise

    # Hosted store: import from the source when given (best-effort -- a failed clone leaves
    # a valid empty repo and reports it), else start empty with an initial commit.
    store = GitStore(default_git_root())
    skey = store_key(ctx.tenant_id, row.key)
    import_status: str | None = None
    if body.source_url:
        try:
            remote = resolve_remote(body.source_url, body.provider)
            userinfo = (
                remote.push_userinfo(body.access_token)
                if remote is not None and body.access_token
                else None
            )
            await store.clone_from(
                skey, body.source_url, token=body.access_token, userinfo=userinfo
            )
            import_status = "imported"
        except GitStoreError as exc:
            await store.ensure_repo(skey)
            import_status = f"import failed: {str(exc)[:160]}"
    else:
        await store.ensure_repo(skey)

    return _response(row, import_status=import_status)


class UpdateRepoRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    source_url: str | None = None
    provider: str | None = None  # 'auto' clears the hint back to auto-detect
    # Write-only, sealed via the Encryptor; replaces the stored credential when given.
    access_token: str | None = None
    runtime: str | None = None
    runtime_image: str | None = None
    registry_username: str | None = None
    registry_token: str | None = None
    setup_cmds: list[str] | None = None
    test_cmd: str | None = None
    clear_test_cmd: bool = False
    build_cmd: str | None = None
    artifact_name: str | None = None
    clear_build: bool = False
    preview_image: str | None = None
    preview_cmd: str | None = None
    preview_port: int | None = None
    preview_env: dict[str, str] | None = None
    clear_preview: bool = False


@router.patch("/{repo_id}")
async def update_repo_endpoint(
    repo_id: uuid.UUID,
    body: UpdateRepoRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> RepoResponse:
    """Update a registration -- notably pointing it at its real remote (source_url + token)
    so delegated branches push back and PRs open there. The hosted store's content is
    untouched; a changed source_url affects future pushes, not history."""
    await require_tenant_permission(ctx, "repo:manage")
    from core.repos.service import get_repo

    row = await get_repo(ctx.tenant_id, repo_id)
    if row is None or row.archived_at is not None:
        raise HTTPException(status_code=404, detail=f"no active repo {repo_id}")
    if (
        body.runtime is not None
        and body.runtime not in RUNTIME_CATALOG
        and body.runtime != "custom"
    ):
        raise HTTPException(status_code=422, detail=f"unknown runtime {body.runtime!r}")
    if body.provider is not None and body.provider not in (*GIT_PROVIDERS, "auto"):
        raise HTTPException(status_code=422, detail=f"unknown provider {body.provider!r}")

    credential_ref: uuid.UUID | None = None
    if body.access_token:
        credential_ref = await store_provider_credential(
            ctx.tenant_id, body.access_token, encryptor=get_encryptor()
        )
    registry_credential_ref = await _seal_registry_credentials(
        ctx.tenant_id, body.registry_username, body.registry_token
    )

    from core.tenancy.scope import tenant_scope

    async with tenant_scope(ctx.tenant_id) as session:
        live = await session.get(RepoRow, repo_id)
        assert live is not None
        if body.name is not None:
            live.name = body.name
        if body.description is not None:
            live.description = body.description
        if body.source_url is not None:
            live.source_url = body.source_url or None
        if body.provider is not None:
            live.provider = None if body.provider == "auto" else body.provider
        if credential_ref is not None:
            live.credential_ref = credential_ref
        if registry_credential_ref is not None:
            live.registry_credential_ref = registry_credential_ref
        if body.runtime is not None:
            live.runtime = body.runtime
        if body.runtime_image is not None:
            live.runtime_image = body.runtime_image.strip() or None
        if body.setup_cmds is not None:
            live.setup_cmds = list(body.setup_cmds)
        if body.clear_test_cmd:
            live.test_cmd = None
        elif body.test_cmd is not None:
            live.test_cmd = body.test_cmd
        if body.clear_build:
            live.build_cmd = None
            live.artifact_name = None
        else:
            if body.build_cmd is not None:
                live.build_cmd = body.build_cmd or None
            if body.artifact_name is not None:
                live.artifact_name = body.artifact_name or None
        if body.clear_preview:
            live.preview_image = None
            live.preview_cmd = None
            live.preview_port = None
            live.preview_env = {}
        else:
            if body.preview_image is not None:
                live.preview_image = body.preview_image.strip() or None
            if body.preview_cmd is not None:
                live.preview_cmd = body.preview_cmd.strip() or None
            if body.preview_port is not None:
                live.preview_port = body.preview_port or None
            if body.preview_env is not None:
                live.preview_env = {str(k): str(v) for k, v in body.preview_env.items()}
        # Validate the override now rather than at launch: a preview that refuses to
        # start is a worse place to learn the port was 99999.
        from core.config import get_settings
        from core.previews.recipe import PreviewRecipeError, resolve_recipe

        try:
            resolve_recipe(
                default_image=get_settings().preview_image,
                repo_overrides={
                    "image": live.preview_image,
                    "cmd": live.preview_cmd,
                    "port": live.preview_port,
                    "env": dict(live.preview_env or {}),
                },
            )
        except PreviewRecipeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await session.flush()
        await session.refresh(live)
        return _response(live)


@router.delete("/{repo_id}", status_code=204)
async def archive_repo_endpoint(
    repo_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> None:
    await require_tenant_permission(ctx, "repo:manage")
    try:
        await archive_repo(ctx.tenant_id, repo_id)
    except RepoNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ── per-persona hosted-git identity (G4.17) ──────────────────────────────────────────
class PersonaCredentialOut(BaseModel):
    persona_id: uuid.UUID
    # Deliberately not the credential_ref, let alone the token: the only thing a caller
    # needs to know is that this persona acts as itself rather than as the repo.
    bound: bool = True


class BindPersonaCredentialRequest(BaseModel):
    persona_id: uuid.UUID
    # Write-only, sealed into a provider_credential on arrival. Never echoed back.
    access_token: str


@router.get("/{repo_id}/persona-credentials")
async def list_persona_credentials_endpoint(
    repo_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> list[PersonaCredentialOut]:
    """Which personas act under their own hosted-git identity on this repo.

    Anything not listed falls back to the repo's own credential -- which is why a
    reviewer persona could not approve a pull request its own identity had opened.
    """
    await require_tenant_permission(ctx, "repo:manage")
    from core.repos.service import list_persona_credentials

    return [
        PersonaCredentialOut(persona_id=pid)
        for pid, _ref in await list_persona_credentials(ctx.tenant_id, repo_id)
    ]


@router.put("/{repo_id}/persona-credentials", status_code=200)
async def bind_persona_credential_endpoint(
    repo_id: uuid.UUID,
    body: BindPersonaCredentialRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> PersonaCredentialOut:
    """Give one persona its own identity on this repo.

    PUT rather than POST: binding the same persona twice is the same binding, so a setup
    script re-run rotates the token instead of colliding on (repo_id, persona_id).
    """
    await require_tenant_permission(ctx, "repo:manage")
    from core.repos.service import bind_persona_credential

    if not body.access_token.strip():
        raise HTTPException(status_code=422, detail="access_token must not be empty")
    credential_ref = await store_provider_credential(
        ctx.tenant_id, body.access_token, encryptor=get_encryptor()
    )
    try:
        await bind_persona_credential(ctx.tenant_id, repo_id, body.persona_id, credential_ref)
    except InvalidRepoError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PersonaCredentialOut(persona_id=body.persona_id)


@router.delete("/{repo_id}/persona-credentials/{persona_id}", status_code=204)
async def unbind_persona_credential_endpoint(
    repo_id: uuid.UUID,
    persona_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
) -> None:
    """Drop a persona's own identity; it falls back to the repo's default credential."""
    await require_tenant_permission(ctx, "repo:manage")
    from core.repos.service import unbind_persona_credential

    if not await unbind_persona_credential(ctx.tenant_id, repo_id, persona_id):
        raise HTTPException(status_code=404, detail="no binding for that persona")


# ── QA build artifact (M-E) ──────────────────────────────────────────────────────────
@router.get("/{repo_id}/artifacts/latest")
async def download_latest_artifact(
    repo_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> Response:
    """The most recent green build's artifact (uploaded by the delegation environment
    after `build_cmd`): the 'deployment for QA' -- a downloadable, playable build."""
    from fastapi.responses import Response

    from api.blob_store_factory import get_blob_store
    from core.ports.blob_store import BlobNotFoundError
    from core.repos.service import get_repo, store_key

    repo = await get_repo(ctx.tenant_id, repo_id)
    if repo is None or not repo.artifact_name:
        raise HTTPException(status_code=404, detail="repo has no build artifact configured")
    blob_key = f"artifacts/{store_key(ctx.tenant_id, repo.key)}/{repo.artifact_name}"
    try:
        data = await get_blob_store().get(blob_key)
    except BlobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="no artifact built yet") from exc
    media = "application/zip" if repo.artifact_name.endswith(".zip") else "application/octet-stream"
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{repo.artifact_name}"'},
    )


# ── playable web build (the artifact, served runnable in the browser) ────────────────
# Godot 4 web exports need cross-origin isolation (SharedArrayBuffer): COOP+COEP on
# every response, plus a correct application/wasm type. The tarball is extracted once
# per content hash into a scratch dir and served from there.
_PLAY_ROOT = "/tmp/pyrrhula-play"
# Shared with the public preview proxy -- see api/play_headers.py for why they must match.
_PLAY_HEADERS = PLAY_HEADERS


async def _extracted_build_dir(tenant_id: uuid.UUID, repo_id: uuid.UUID) -> Path:
    import hashlib
    import io
    import tarfile

    from api.blob_store_factory import get_blob_store
    from core.ports.blob_store import BlobNotFoundError
    from core.repos.service import get_repo, store_key

    repo = await get_repo(tenant_id, repo_id)
    if repo is None or not repo.artifact_name:
        raise HTTPException(status_code=404, detail="repo has no build artifact configured")
    if not repo.artifact_name.endswith((".tar.gz", ".tgz")):
        raise HTTPException(
            status_code=409,
            detail="playable serving expects a .tar.gz artifact (a web export)",
        )
    blob_key = f"artifacts/{store_key(tenant_id, repo.key)}/{repo.artifact_name}"
    try:
        data = await get_blob_store().get(blob_key)
    except BlobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="no artifact built yet") from exc

    digest = hashlib.sha256(data).hexdigest()[:24]
    target = Path(_PLAY_ROOT) / digest
    if not target.is_dir():
        tmp = Path(_PLAY_ROOT) / f".{digest}.partial"
        tmp.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            archive.extractall(tmp, filter="data")  # refuses traversal/links (PEP 706)
        tmp.rename(target)
    return target


@router.get("/{repo_id}/artifacts/play")
async def play_latest_artifact_index(
    repo_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> RedirectResponse:
    from fastapi.responses import RedirectResponse

    await _extracted_build_dir(ctx.tenant_id, repo_id)  # 404/409 now, not on the redirect
    return RedirectResponse(url=f"/api/repos/{repo_id}/artifacts/play/index.html")


@router.get("/{repo_id}/artifacts/play/{path:path}")
async def play_latest_artifact_file(
    repo_id: uuid.UUID, path: str, ctx: RequestContext = Depends(get_request_context)
) -> Response:

    from fastapi.responses import Response

    build_dir = await _extracted_build_dir(ctx.tenant_id, repo_id)
    resolved = (build_dir / path).resolve()
    if not str(resolved).startswith(str(build_dir.resolve())) or not resolved.is_file():
        raise HTTPException(status_code=404, detail="no such file in the build")
    return Response(
        content=resolved.read_bytes(),
        media_type=content_type_for(path),
        headers=_PLAY_HEADERS,
    )
