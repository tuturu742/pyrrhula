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
from typing import Any, cast

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
from core.agents.authoring import resolve_connection_api_key, store_provider_credential
from core.repos.models import RepoRow
from core.repos.service import (
    GIT_PROVIDERS,
    InvalidRepoError,
    RepoNotFoundError,
    archive_repo,
    create_repo,
    get_repo,
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
    # What the remote calls its default branch, and what delegated work targets.
    default_branch: str = "main"
    has_credential: bool = False
    runtime: str = "debian"
    runtime_image: str | None = None
    has_registry_credential: bool = False
    setup_cmds: list[str] = []
    test_cmd: str | None = None
    build_cmd: str | None = None
    artifact_name: str | None = None
    # The operator's half of the preview recipe. `_response` has always passed these and
    # the model has never declared them, so Pydantic dropped all four silently: setting a
    # preview image or command succeeded and then read back as if nothing had been set,
    # which leaves the UI unable to show what a repo is configured to do.
    preview_image: str | None = None
    preview_cmd: str | None = None
    preview_port: int | None = None
    preview_env: dict[str, str] = {}
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
        default_branch=row.default_branch,
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
    setup: list[str] = []
    # Whether this tenant registered it. A built-in overridden by the tenant reports
    # True: what runs is the tenant's image, and a catalog that hid that would be lying
    # about which image a build uses.
    tenant_owned: bool = False


class RegisterRuntimeRequest(BaseModel):
    image: str
    setup: list[str] = []


@router.get("/runtimes")
async def list_runtimes_endpoint(
    ctx: RequestContext = Depends(get_request_context),
) -> list[RuntimeResponse]:
    """Every runtime this tenant may build in: the deployment's built-ins, this tenant's
    own registrations on top, plus the 'custom' sentinel (bring your own image)."""
    from core.repos.runtimes import BUILTIN_RUNTIMES, CUSTOM, resolved_runtimes

    effective = await resolved_runtimes(ctx.tenant_id)
    entries = [
        RuntimeResponse(
            key=k,
            image=str(v["image"]),
            setup=[str(c) for c in cast("list[object]", v.get("setup") or [])],
            tenant_owned=BUILTIN_RUNTIMES.get(k) != v,
        )
        for k, v in sorted(effective.items())
    ]
    entries.append(RuntimeResponse(key=CUSTOM, image=""))
    return entries


@router.put("/runtimes/{key}")
async def register_runtime_endpoint(
    key: str,
    body: RegisterRuntimeRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> RuntimeResponse:
    """Register (or replace) one of this tenant's runtimes.

    Naming a key that a built-in already uses overrides it for this tenant -- which is
    how a deployment behind an internal registry points ``debian`` at its own mirror once
    instead of every repo carrying the mirror's address."""
    from core.repos.runtimes import InvalidRuntimeError, register_runtime

    await require_tenant_permission(ctx, "repo:manage")
    try:
        entry = await register_runtime(ctx.tenant_id, key, body.image, body.setup)
    except InvalidRuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RuntimeResponse(
        key=key,
        image=str(entry["image"]),
        setup=[str(c) for c in cast("list[object]", entry.get("setup") or [])],
        tenant_owned=True,
    )


@router.delete("/runtimes/{key}", status_code=204)
async def remove_runtime_endpoint(
    key: str, ctx: RequestContext = Depends(get_request_context)
) -> None:
    """Forget one of this tenant's runtimes. A built-in of the same name becomes visible
    again -- removing an override is how a tenant goes back to the deployment's image.

    Repos already pinned to the key keep the name and resolve to whatever it means now,
    which for a removed non-built-in is nothing: that build is refused with an unknown
    runtime rather than silently running on some other image."""
    from core.repos.runtimes import remove_runtime

    await require_tenant_permission(ctx, "repo:manage")
    if not await remove_runtime(ctx.tenant_id, key):
        raise HTTPException(status_code=404, detail=f"this tenant has no runtime {key!r}")


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


def _checked_image(value: str | None, *, field: str) -> str | None:
    """Refuse an unusable image reference here, rather than at pull time inside a job.

    A registry web page pasted from the address bar is the common case and looks
    plausible in the form; the engine's failure for it surfaces far from this field."""
    from core.repos.image_ref import ImageRefError, normalise_image_ref

    try:
        return normalise_image_ref(value)
    except ImageRefError as exc:
        raise HTTPException(status_code=422, detail=f"{field}: {exc}") from exc


async def _seal_registry_credentials(
    tenant_id: uuid.UUID, username: str | None, token: str | None, image: str | None
) -> uuid.UUID | None:
    """Registry credentials (for private runtime images) sealed as one encrypted JSON
    payload -- same store and write-only discipline as repo access tokens.

    The payload records **which registry** the credential is for. Without that, the
    credential was sent to whatever registry the delegation's image named -- and the image
    can come from ``pyrrhula-build.json`` inside the repository, so a commit could redirect
    the repo's registry password to a host of its choosing.
    """
    if not token:
        return None
    if not image:
        raise HTTPException(
            status_code=422,
            detail="registry credentials need the image they are for: set runtime_image too",
        )
    import json as _json

    from core.repos.image_ref import registry_host

    payload = _json.dumps(
        {"username": username or "", "password": token, "serveraddress": registry_host(image)}
    )
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
        ctx.tenant_id, body.registry_username, body.registry_token, body.runtime_image
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
            runtime_image=_checked_image(body.runtime_image, field="runtime_image"),
            registry_credential_ref=registry_credential_ref,
            setup_cmds=body.setup_cmds,
            test_cmd=body.test_cmd,
            build_cmd=body.build_cmd,
            artifact_name=body.artifact_name,
            preview_image=_checked_image(body.preview_image, field="preview_image"),
            preview_cmd=body.preview_cmd,
            preview_port=body.preview_port,
            preview_env=body.preview_env,
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
            imported_branch = await store.clone_from(
                skey, body.source_url, token=body.access_token, userinfo=userinfo
            )
            import_status = "imported"
            # The remote's own name for its default branch, not an assumption. Stored
            # now because nothing can recover it later without asking the remote again.
            if imported_branch and imported_branch != row.default_branch:
                from core.tenancy.scope import tenant_scope

                async with tenant_scope(ctx.tenant_id) as session:
                    live = await session.get(type(row), row.id)
                    if live is not None:
                        live.default_branch = imported_branch
                row.default_branch = imported_branch
        except GitStoreError as exc:
            await store.ensure_repo(skey)
            import_status = f"import failed: {str(exc)[:160]}"
    else:
        await store.ensure_repo(skey)

    return _response(row, import_status=import_status)


class UpdateRepoRequest(BaseModel):
    name: str | None = None
    # The branch delegated work targets. Point it at a release-candidate branch to put
    # agents on stabilisation work without moving the project's trunk.
    default_branch: str | None = None
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


async def _ensure_store_branch(tenant_id: uuid.UUID, repo: Any, branch: str) -> None:
    """A branch a repository points at has to exist in the hosted store.

    Setting the row alone would leave every later checkout failing on a name nothing
    ever created -- the row would say `rc/0.1.0` and the store would have no such
    branch. Created from the current one when absent, which is what cutting a
    release-candidate branch means.
    """
    store = GitStore(default_git_root())
    skey = store_key(tenant_id, repo.key)
    try:
        if await store.branch_exists(skey, branch):
            return
        await store.create_branch(skey, branch, base=repo.default_branch)
    except GitStoreError as exc:
        raise HTTPException(
            status_code=409, detail=f"could not prepare branch {branch!r}: {exc}"
        ) from exc


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
    if body.runtime is not None:
        from core.repos.runtimes import CUSTOM, resolved_runtimes

        known = await resolved_runtimes(ctx.tenant_id)
        if body.runtime not in known and body.runtime != CUSTOM:
            raise HTTPException(status_code=422, detail=f"unknown runtime {body.runtime!r}")
    if body.provider is not None and body.provider not in (*GIT_PROVIDERS, "auto"):
        raise HTTPException(status_code=422, detail=f"unknown provider {body.provider!r}")

    credential_ref: uuid.UUID | None = None
    if body.access_token:
        credential_ref = await store_provider_credential(
            ctx.tenant_id, body.access_token, encryptor=get_encryptor()
        )
    credential_image = body.runtime_image
    if body.registry_token and not credential_image:
        existing = await get_repo(ctx.tenant_id, repo_id)
        credential_image = existing.runtime_image if existing is not None else None
    registry_credential_ref = await _seal_registry_credentials(
        ctx.tenant_id, body.registry_username, body.registry_token, credential_image
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
        if body.default_branch:
            await _ensure_store_branch(ctx.tenant_id, live, body.default_branch)
            live.default_branch = body.default_branch
        if body.runtime is not None:
            live.runtime = body.runtime
        if body.runtime_image is not None:
            live.runtime_image = _checked_image(body.runtime_image, field="runtime_image")
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
                live.preview_image = _checked_image(body.preview_image, field="preview_image")
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


class RefreshRepoResponse(BaseModel):
    """What a refresh did. ``status`` is ``unchanged`` | ``fast_forwarded`` | ``ahead``
    | ``diverged`` -- see ``GitStore.fetch_from``."""

    status: str
    before_sha: str
    after_sha: str
    behind: int = 0
    ahead: int = 0
    detail: str


_REFRESH_DETAIL = {
    "unchanged": "Already up to date with the source.",
    "fast_forwarded": "Pulled {behind} new commit(s) from the source.",
    "ahead": (
        "This repository is {ahead} commit(s) ahead of its source -- work merged here "
        "that has not been pushed out. Nothing to pull."
    ),
    "diverged": (
        "This repository and its source have both moved ({ahead} here, {behind} there). "
        "Nothing was changed: commits here may be merged pull requests that exist "
        "nowhere else, so which side wins is your call, not a default."
    ),
}


@router.post("/{repo_id}/refresh")
async def refresh_repo_endpoint(
    repo_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> RefreshRepoResponse:
    """Re-read the external source into the hosted store.

    Registration imports once and never looked again, so a repository registered from
    GitHub described whatever it held that day, for ever. Fast-forward only: the store
    is not a mirror -- approved pull requests merge into ``main`` here -- so a refresh
    that could rewind is not a refresh, it is data loss.
    """
    await require_tenant_permission(ctx, "repo:manage")
    repo = await get_repo(ctx.tenant_id, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail=f"no repo {repo_id} in this tenant")
    if not repo.source_url:
        raise HTTPException(
            status_code=400,
            detail="this repository has no source to refresh from (it was created empty)",
        )

    token = await resolve_connection_api_key(
        ctx.tenant_id,
        str(repo.credential_ref) if repo.credential_ref else None,
        encryptor=get_encryptor(),
    )
    remote = resolve_remote(repo.source_url, repo.provider)
    userinfo = remote.push_userinfo(token) if remote is not None and token else None

    store = GitStore(default_git_root())
    try:
        result = await store.fetch_from(
            store_key(ctx.tenant_id, repo.key),
            repo.source_url,
            token=token,
            userinfo=userinfo,
            local_branch=repo.default_branch,
        )
    except GitStoreError as exc:
        # GitStoreError already redacts credentials from git's own message.
        raise HTTPException(status_code=502, detail=f"could not reach the source: {exc}") from exc

    return RefreshRepoResponse(
        status=result.status,
        before_sha=result.before_sha,
        after_sha=result.after_sha,
        behind=result.behind,
        ahead=result.ahead,
        detail=_REFRESH_DETAIL[result.status].format(behind=result.behind, ahead=result.ahead),
    )


@router.delete("/{repo_id}", status_code=204)
async def archive_repo_endpoint(
    repo_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> None:
    await require_tenant_permission(ctx, "repo:manage")
    try:
        await archive_repo(ctx.tenant_id, repo_id)
    except RepoNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ── per-persona hosted-git identity  ──────────────────────────────────────────
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
class PullRequestOut(BaseModel):
    branch: str
    pr_ref: str = ""
    title: str = ""
    status: str = ""
    ci_status: str = ""
    commits: int = 0
    # Whether this branch has a build to preview. A pull request whose branch has been
    # deleted, or which never produced an artifact, still has a record in the sidecar --
    # offering it as previewable would deploy straight to a 404.
    previewable: bool = False


@router.get("/{repo_id}/pull-requests")
async def list_repo_pull_requests(
    repo_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> list[PullRequestOut]:
    """The pull requests a session's delegations opened, so a human can pick one.

    Multi-pull-request sessions are the normal case -- one work item each, in parallel --
    and until artifacts were keyed by ref there was nothing to pick *between*: every
    branch wrote one artifact slot per repository. Now each branch has its own build, and
    this is the list of what can be previewed.
    """
    from adapters.mcp.git_store import GitStore, default_git_root
    from api.blob_store_factory import get_blob_store
    from core.ports.blob_store import BlobNotFoundError
    from core.repos.service import artifact_blob_key, get_repo, store_key

    repo = await get_repo(ctx.tenant_id, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="no such repo")
    key = store_key(ctx.tenant_id, repo.key)
    store = GitStore(default_git_root())

    out: list[PullRequestOut] = []
    for record in await store.list_prs(key):
        branch = str(record.get("branch") or "")
        if not branch or not await store.branch_exists(key, branch):
            continue
        previewable = False
        if repo.artifact_name:
            try:
                await get_blob_store().get(artifact_blob_key(key, repo.artifact_name, branch))
                previewable = True
            except BlobNotFoundError:
                previewable = False
        out.append(
            PullRequestOut(
                branch=branch,
                pr_ref=str(record.get("pr_ref") or ""),
                title=str(record.get("title") or ""),
                status=str(record.get("status") or ""),
                ci_status=str(record.get("ci_status") or ""),
                commits=int(record.get("commits") or 0),
                previewable=previewable,
            )
        )
    return out


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
