"""Repo registry row operations + the curated exec-runtime catalog.

Rows only -- the hosted git store and exec environments are adapters, composed at the API /
worker layer (same discipline as the Encryptor: core never imports an adapter). The runtime
catalog is data: a registrant picks a key; the exec-env adapter maps it to an image + baseline
setup. Users never supply raw images -- the catalog is the allowlist.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from core.repos.models import PersonaGitCredentialRow, RepoRow, SessionRepoRow
from core.tenancy.scope import tenant_scope

_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,62}")

# Hosted-git provider hints a registration may carry (NULL = auto-detect by host).
# Mirrors adapters/gitremote/registry.PROVIDER_KEYS -- duplicated as data here because
# core must not import adapters (composition roots wire them together).
GIT_PROVIDERS = ("github", "gitlab", "gitea", "generic")

# runtime key -> {image, baseline setup} (git must end up present in every runtime; the
# clone happens inside the environment).
RUNTIME_CATALOG: dict[str, dict[str, object]] = {
    "debian": {
        "image": "docker.io/library/debian:bookworm",
        "setup": [
            "apt-get update && apt-get install -y --no-install-recommends git ca-certificates"
        ],
    },
    "node20": {"image": "docker.io/library/node:20-bookworm", "setup": []},
    "python312": {"image": "docker.io/library/python:3.12-bookworm", "setup": []},
    "java21": {
        "image": "docker.io/library/eclipse-temurin:21-jdk",
        "setup": [
            "apt-get update && apt-get install -y --no-install-recommends git ca-certificates"
        ],
    },
}


async def resolve_git_identity(
    tenant_id: uuid.UUID, repo_id: uuid.UUID, persona_id: uuid.UUID | None
) -> uuid.UUID | None:
    """The credential a persona acts under on this repo's hosted remote (G4.17).

    The persona's own binding if it has one, else the repo's default ``credential_ref``.
    One function so every call site -- push, comment, review, merge -- agrees on the
    fallback, and so "this bot" vs "the repo bot" is decided in exactly one place.
    Returns a ``provider_credential`` id (still sealed; the caller decrypts transiently),
    or None when neither the persona nor the repo has a credential.
    """
    async with tenant_scope(tenant_id) as session:
        if persona_id is not None:
            bound = await session.scalar(
                select(PersonaGitCredentialRow.credential_ref).where(
                    PersonaGitCredentialRow.repo_id == repo_id,
                    PersonaGitCredentialRow.persona_id == persona_id,
                )
            )
            if bound is not None:
                return bound
        repo = await session.get(RepoRow, repo_id)
        return repo.credential_ref if repo is not None else None


def mint_git_job_token(store_key_value: str, *, ttl_seconds: int = 7200) -> str:
    """A short-lived credential for one hosted store repo over git smart-HTTP -- what a
    delegated exec environment (local container, k8s Job, cloud runner) puts in its
    clone/push URL instead of mounting the store volume. Scope: exactly one store_key;
    signed with the deployment's jwt_secret; never persisted."""
    import time as _time

    import jwt as _jwt

    from core.config import get_settings

    settings = get_settings()
    return _jwt.encode(
        {"sk": store_key_value, "exp": int(_time.time()) + ttl_seconds, "use": "git"},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def verify_git_job_token(token: str, store_key_value: str) -> bool:
    import jwt as _jwt

    from core.config import get_settings

    settings = get_settings()
    try:
        claims = _jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except _jwt.PyJWTError:
        return False
    return claims.get("use") == "git" and claims.get("sk") == store_key_value


def mint_artifact_read_token(store_key_value: str, artifact_name: str, *, ttl_seconds: int) -> str:
    """Read one named build artifact, and nothing else.

    Deliberately NOT ``mint_git_job_token``: that token authorizes ``git-receive-pack``,
    i.e. push. A preview container is reachable through an anonymous public link and
    serves untrusted built output, so giving it a repo-write credential would be a real
    escalation. The distinct ``use`` claim means neither verifier accepts the other's
    token."""
    import time as _time

    import jwt as _jwt

    from core.config import get_settings

    settings = get_settings()
    return _jwt.encode(
        {
            "use": "artifact",
            "sk": store_key_value,
            "n": artifact_name,
            "exp": int(_time.time()) + ttl_seconds,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def verify_artifact_read_token(token: str, store_key_value: str, artifact_name: str) -> bool:
    import jwt as _jwt

    from core.config import get_settings

    settings = get_settings()
    try:
        claims = _jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except _jwt.PyJWTError:
        return False
    return (
        claims.get("use") == "artifact"
        and claims.get("sk") == store_key_value
        and claims.get("n") == artifact_name
    )


def store_key(tenant_id: uuid.UUID, repo_key: str) -> str:
    """The hosted git store's directory for a repo. Tenant-prefixed: repo keys are unique
    per tenant, so a bare key would collide across tenants in the shared store."""
    return f"t{str(tenant_id)[:8]}-{repo_key}"


class RepoNotFoundError(Exception):
    pass


class InvalidRepoError(Exception):
    pass


async def create_repo(
    tenant_id: uuid.UUID,
    key: str,
    name: str,
    *,
    description: str = "",
    source_url: str | None = None,
    provider: str | None = None,
    credential_ref: uuid.UUID | None = None,
    runtime: str = "debian",
    runtime_image: str | None = None,
    registry_credential_ref: uuid.UUID | None = None,
    setup_cmds: list[str] | None = None,
    test_cmd: str | None = None,
    build_cmd: str | None = None,
    artifact_name: str | None = None,
    created_by: uuid.UUID | None = None,
) -> RepoRow:
    if not _KEY_RE.fullmatch(key):
        raise InvalidRepoError(
            f"invalid repo key {key!r}: lowercase letters/digits/-/_, 2..63 chars"
        )
    if runtime not in RUNTIME_CATALOG and runtime != "custom":
        raise InvalidRepoError(
            f"unknown runtime {runtime!r}; pick one of {sorted(RUNTIME_CATALOG)} or 'custom'"
        )
    if runtime == "custom" and not (runtime_image or "").strip():
        raise InvalidRepoError("runtime 'custom' requires a runtime_image")
    if provider is not None and provider not in GIT_PROVIDERS:
        raise InvalidRepoError(
            f"unknown git provider {provider!r}; pick one of {sorted(GIT_PROVIDERS)}"
        )
    async with tenant_scope(tenant_id) as session:
        row = RepoRow(
            tenant_id=tenant_id,
            key=key,
            name=name,
            description=description,
            source_url=source_url,
            provider=provider,
            credential_ref=credential_ref,
            runtime=runtime,
            runtime_image=(runtime_image or "").strip() or None,
            registry_credential_ref=registry_credential_ref,
            setup_cmds=list(setup_cmds or []),
            test_cmd=test_cmd,
            build_cmd=build_cmd,
            artifact_name=artifact_name,
            created_by=created_by,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


async def list_repos(tenant_id: uuid.UUID, *, include_archived: bool = False) -> list[RepoRow]:
    async with tenant_scope(tenant_id) as session:
        stmt = select(RepoRow).where(RepoRow.tenant_id == tenant_id)
        if not include_archived:
            stmt = stmt.where(RepoRow.archived_at.is_(None))
        rows = (await session.execute(stmt.order_by(RepoRow.name))).scalars()
        return list(rows)


async def get_repo(tenant_id: uuid.UUID, repo_id: uuid.UUID) -> RepoRow | None:
    async with tenant_scope(tenant_id) as session:
        return await session.get(RepoRow, repo_id)


async def archive_repo(tenant_id: uuid.UUID, repo_id: uuid.UUID) -> None:
    """Soft-delete: the registry row leaves the list; the hosted store's content stays."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(RepoRow, repo_id)
        if row is None:
            raise RepoNotFoundError(f"no repo {repo_id} in this tenant")
        if row.archived_at is None:
            row.archived_at = datetime.now(UTC)
        await session.flush()


async def set_session_repos(
    tenant_id: uuid.UUID, session_id: uuid.UUID, repo_ids: list[uuid.UUID]
) -> list[RepoRow]:
    """Pin a session's repo selection (#repos). Validates each repo exists and is not
    archived; returns the rows so the caller can register their git servers."""
    rows: list[RepoRow] = []
    async with tenant_scope(tenant_id) as session:
        for repo_id in repo_ids:
            repo = await session.get(RepoRow, repo_id)
            if repo is None or repo.archived_at is not None:
                raise RepoNotFoundError(f"no active repo {repo_id} in this tenant")
            rows.append(repo)
            session.add(SessionRepoRow(tenant_id=tenant_id, session_id=session_id, repo_id=repo_id))
        await session.flush()
        for repo in rows:
            session.expunge(repo)
    return rows


async def list_session_repos(tenant_id: uuid.UUID, session_id: uuid.UUID) -> list[RepoRow]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(RepoRow)
                .join(SessionRepoRow, SessionRepoRow.repo_id == RepoRow.id)
                .where(SessionRepoRow.session_id == session_id)
                .order_by(RepoRow.name)
            )
        ).scalars()
        return list(rows)
