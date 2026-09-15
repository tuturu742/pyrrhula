"""Repo registry (D15 follow-on): repos as first-class tenant resources.

"Repo" in core follows the precedent ``core/knowledge/repo_ingestion.py`` set once D15 made
delegated coding work a platform concern (v1.2's ``purpose='delegation'``): the repository is
infrastructure the platform hosts, not pack-domain vocabulary. A row names a repo in the
server-side git store (its ``key`` is the store's directory), an optional import source, an
optional encrypted access credential (``credential_ref`` -> ``provider_credential`` row --
never a token in this table), and the exec-environment config agents build/test in
(``runtime`` from the curated catalog, ``setup_cmds``, ``test_cmd``).

``session_repo`` mirrors ``session_persona``: the explicit per-session selection. Delegation
may only target a session's selected repos.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

# PersonaGitCredentialRow's FK targets `persona`, which is mapped in core.agents.models.
# SQLAlchemy resolves a FK target through the shared metadata when it orders an INSERT, so
# a process that had imported only this module raised NoReferencedTableError on the first
# write -- the target table was simply never registered. It worked in the api because some
# other import happened to pull the mapper in first, which is luck, not a dependency.
# Importing it here states the dependency outright. (No cycle: core.agents.models does not
# import core.repos, and both package __init__ files are empty.)
from core.agents.models import Persona as _Persona  # noqa: F401
from core.tenancy.models import Base


class RepoRow(Base):
    __tablename__ = "repo"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # Optional import source (public URL or file:// path); cloned into the hosted store
    # best-effort at registration.
    source_url: Mapped[str | None] = mapped_column(String(1023), nullable=True)
    # Which hosted-git API the remote speaks: github|gitlab|gitea|generic. NULL = auto-
    # detect by host (public hosts only) -- self-hosted GitLab/Gitea set it explicitly.
    provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Opaque id of a provider_credential row (Encryptor-sealed access token). Never a token.
    credential_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Exec-environment config: curated runtime key + one-time setup + the test command.
    runtime: Mapped[str] = mapped_column(
        String(31), nullable=False, default="debian", server_default=text("'debian'")
    )
    # Custom image ref (docker.io/..., ghcr.io/...). Set => overrides the catalog entry;
    # the image must contain git (an env preflight fails loudly otherwise).
    runtime_image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Opaque pointer to encrypted registry credentials (JSON {username, password} sealed
    # via the Encryptor) for pulling private images. Never credentials themselves.
    registry_credential_ref: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    setup_cmds: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    test_cmd: Mapped[str | None] = mapped_column(String(511), nullable=True)
    # QA build artifact (M-E): after green tests a delegation runs build_cmd and, when
    # the named artifact file exists, uploads it to the blob store -- served back at
    # GET /repos/{id}/artifacts/latest. Both unset = no build step.
    build_cmd: Mapped[str | None] = mapped_column(String(511), nullable=True)
    artifact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Preview recipe overrides. NULL/empty means "whatever pyrrhula-preview.json in the
    # repo says", and failing that the platform's static-site server -- see
    # core/previews/recipe.py for the precedence and why the artifact fetch is not part
    # of what a recipe may replace.
    preview_image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    preview_cmd: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    preview_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preview_env: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_repo_tenant_key"),)


class SessionRepoRow(Base):
    __tablename__ = "session_repo"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repo.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("session_id", "repo_id", name="uq_session_repo"),)


class PersonaGitCredentialRow(Base):
    """Which hosted-git identity a persona acts under, per repo (G4.17).

    Without a row here a persona falls back to the repo's own ``credential_ref``, so every
    action is one platform identity — which is why a reviewer bot cannot file a formal
    approval on a PR its own identity opened. A row binds this persona, for this repo, to a
    distinct ``provider_credential`` (Encryptor-sealed — **never a token in this table**,
    exactly as ``RepoRow.credential_ref``), so review and merge happen under separate
    identities the host's gate can tell apart.

    Tenant-scoped: RLS with FORCE, the standard ``tenant_isolation`` policy shape, opened
    only through ``tenant_scope()`` (CLAUDE.md rule 4).
    """

    __tablename__ = "persona_git_credential"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repo.id", ondelete="CASCADE"), nullable=False
    )
    persona_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("persona.id", ondelete="CASCADE"), nullable=False
    )
    # Opaque id of a provider_credential row (Encryptor-sealed token). Never a token.
    credential_ref: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("repo_id", "persona_id", name="uq_persona_git_credential_repo_persona"),
    )
