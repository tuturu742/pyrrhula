"""Repo registry row operations + the curated exec-runtime catalog.

Rows only -- the hosted git store and exec environments are adapters, composed at the API /
worker layer (same discipline as the Encryptor: core never imports an adapter). The runtime
catalog is data: a registrant picks a key; the exec-env adapter maps it to an image + baseline
setup. Users never supply raw images -- the catalog is the allowlist.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from core.repos.models import PersonaGitCredentialRow, RepoRow, SessionRepoRow
from core.repos.runtimes import BUILTIN_RUNTIMES, CUSTOM, resolved_runtimes
from core.tenancy.scope import tenant_scope

_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,62}")

# Hosted-git provider hints a registration may carry (NULL = auto-detect by host).
# Mirrors adapters/gitremote/registry.PROVIDER_KEYS -- duplicated as data here because
# core must not import adapters (composition roots wire them together).
GIT_PROVIDERS = ("github", "gitlab", "gitea", "generic")

# runtime key -> {image, baseline setup} (git must end up present in every runtime; the
# clone happens inside the environment).
# The deployment's built-in runtimes. The *effective* catalog is per tenant -- a tenant
# registers its own images and may override any of these by key -- so read
# ``core.repos.runtimes.resolved_runtimes`` rather than this. Kept as a name because it
# is the floor every tenant starts from.
RUNTIME_CATALOG = BUILTIN_RUNTIMES


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


async def list_persona_credentials(
    tenant_id: uuid.UUID, repo_id: uuid.UUID
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """(persona_id, credential_ref) for every persona bound to its own identity on this
    repo. The ref stays opaque -- callers report *that* a binding exists, never the token.
    """
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(
                    PersonaGitCredentialRow.persona_id,
                    PersonaGitCredentialRow.credential_ref,
                ).where(PersonaGitCredentialRow.repo_id == repo_id)
            )
        ).all()
    return [(r[0], r[1]) for r in rows]


async def bind_persona_credential(
    tenant_id: uuid.UUID,
    repo_id: uuid.UUID,
    persona_id: uuid.UUID,
    credential_ref: uuid.UUID,
) -> None:
    """Bind a persona to its own hosted-git identity on this repo, or rebind an existing
    one. Upsert on (repo_id, persona_id) -- re-running a setup script must rotate the
    binding rather than collide with the unique constraint.

    ``credential_ref`` points at a sealed ``provider_credential``; a raw token must never
    reach this table (see PersonaGitCredentialRow).
    """
    async with tenant_scope(tenant_id) as session:
        if await session.get(RepoRow, repo_id) is None:
            raise InvalidRepoError(f"no repo {repo_id}")
        existing = await session.scalar(
            select(PersonaGitCredentialRow).where(
                PersonaGitCredentialRow.repo_id == repo_id,
                PersonaGitCredentialRow.persona_id == persona_id,
            )
        )
        if existing is None:
            session.add(
                PersonaGitCredentialRow(
                    tenant_id=tenant_id,
                    repo_id=repo_id,
                    persona_id=persona_id,
                    credential_ref=credential_ref,
                )
            )
        else:
            existing.credential_ref = credential_ref


async def unbind_persona_credential(
    tenant_id: uuid.UUID, repo_id: uuid.UUID, persona_id: uuid.UUID
) -> bool:
    """Drop a persona's own identity so it falls back to the repo's default credential.
    Returns whether a binding was actually removed."""
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(PersonaGitCredentialRow).where(
                PersonaGitCredentialRow.repo_id == repo_id,
                PersonaGitCredentialRow.persona_id == persona_id,
            )
        )
        if existing is None:
            return False
        await session.delete(existing)
        return True


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


# Artifacts used to be addressed as ``artifacts/<store>/<name>`` with "latest" semantics
# -- each green build replaced the last. `artifact_name` comes from the repo's own config,
# so it is one fixed string per repository and every branch wrote to the same key: two
# delegations running at once silently overwrote each other, and whatever finished last was
# what a preview served, with nothing anywhere recording that it had happened. A ref segment
# is what gives concurrent branches somewhere separate to land.
_REF_SLUG_RE = re.compile(r"[^a-z0-9]+")


def artifact_ref_slug(git_ref: str) -> str:
    """A filesystem- and hostname-safe stand-in for a git ref.

    Suffixed with a digest of the original because slugging is lossy: `feat/login` and
    `feat-login` flatten to the same characters and must not end up sharing a blob key or a
    container name. The readable half is kept short so the whole thing still fits a DNS
    label once a preview name is built around it.
    """
    cleaned = _REF_SLUG_RE.sub("-", git_ref.strip().lower()).strip("-")
    digest = hashlib.sha256(git_ref.encode()).hexdigest()[:8]
    return f"{cleaned[:24].strip('-')}-{digest}" if cleaned else digest


def artifact_blob_key(store_key_value: str, artifact_name: str, git_ref: str = "") -> str:
    """Where one build's artifact lives.

    An empty ``git_ref`` keeps the pre-ref layout, so artifacts uploaded before this
    existed stay readable and a caller that genuinely has no ref in hand still works. Every
    caller that knows its branch supplies it, which is what stops the overwrite.
    """
    if not git_ref:
        return f"artifacts/{store_key_value}/{artifact_name}"
    return f"artifacts/{store_key_value}/refs/{artifact_ref_slug(git_ref)}/{artifact_name}"


def mint_artifact_read_token(
    store_key_value: str, artifact_name: str, *, ttl_seconds: int, git_ref: str = ""
) -> str:
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
            # Scoped to the ref as well as the name: a preview of one branch must not be
            # able to read another branch's build just by asking for it.
            "r": git_ref,
            "exp": int(_time.time()) + ttl_seconds,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def verify_artifact_read_token(
    token: str, store_key_value: str, artifact_name: str, git_ref: str = ""
) -> bool:
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
        and claims.get("r", "") == git_ref
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
    default_branch: str = "main",
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
    known = await resolved_runtimes(tenant_id)
    if runtime not in known and runtime != CUSTOM:
        raise InvalidRepoError(
            f"unknown runtime {runtime!r}; pick one of {sorted(known)} or {CUSTOM!r}, "
            "or register it first"
        )
    if runtime == CUSTOM and not (runtime_image or "").strip():
        raise InvalidRepoError(f"runtime {CUSTOM!r} requires a runtime_image")
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
            default_branch=default_branch or "main",
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
