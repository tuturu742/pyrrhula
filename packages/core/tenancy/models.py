"""Tenancy, identity, and access data model (plan §12.1).

``†`` in the plan means "tenant-scoped, RLS-covered" — every such table here carries an
explicit ``tenant_id`` column (denormalised onto child tables too, e.g. ``identity``, so
the RLS predicate never has to join through ``principal`` to be enforced) and gets a
``FORCE ROW LEVEL SECURITY`` policy in the migration. ``tenant`` and ``role_permission``
are the two tables in this module that are deliberately *not* tenant-scoped: ``tenant`` is
the root of scoping, and ``role_permission`` is global system policy data, not tenant data.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )


class Tenant(Base):
    """The root of tenant scoping. Not itself RLS-covered — there is no "other tenant"
    for the tenant table to hide rows from."""

    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'shared' (RLS in one DB) | 'schema' | 'database' — D11/H5.6 escalation hook.
    isolation_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="shared")
    # Read by the TenantRouter port (T0.3); v1 impl ignores it and returns one DSN.
    region: Mapped[str] = mapped_column(String(63), nullable=False, default="default")
    settings: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    # A1.10 (D13): the one reserved, read-only tenant holding pack seed content. No
    # memberships ever exist for it; this flag is the explicit, defense-in-depth guard
    # ``resolve_tenant_for_auth`` checks so register/login can never target it even if
    # that otherwise-true absence of memberships weren't the case.
    is_library: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Soft-deactivation (migration c4f2a7e1b9d3): NULL = active, a timestamp = deactivated.
    # Set from the admin console (separate port); auth rejects a deactivated tenant so no
    # principal in it can log in or make requests. Not a delete -- the superuser purge CLI
    # is the only path that truly removes a tenant.
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "isolation_mode IN ('shared', 'schema', 'database')", name="ck_tenant_isolation_mode"
        ),
    )


class Principal(Base):
    """Unifies humans, service accounts, and agents. Everything downstream references
    ``principal``, never a human-specific "user" table (D11) — SSO in Phase 5 adds an
    ``identity`` row, not a new FK everywhere."""

    __tablename__ = "principal"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # human|service|agent
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('human', 'service', 'agent')", name="ck_principal_kind"),
    )


class Identity(Base):
    """How a human principal authenticates. ``tenant_id`` is denormalised from
    ``principal`` so the RLS predicate on this table never needs a join to be enforced."""

    __tablename__ = "identity"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principal.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)  # local|oidc|saml
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Only populated for provider='local'. Argon2 hash, never a plaintext or reversible value.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Scoped by tenant, not global. An email identifies a person *within* one tenant;
    # making it unique across the whole deployment coupled tenants that are otherwise
    # independent -- the same human could not own accounts in two organizations, and
    # provisioning one tenant could fail because of a row in another one they cannot see.
    # Every lookup is already tenant-scoped (verify_local takes a tenant_id, and RLS
    # constrains the rest), so nothing relied on the global form.
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "external_id", name="uq_identity_tenant_provider_ext"
        ),
    )


class Membership(Base):
    """Tenant-level role: owner|admin|editor|participant|viewer (requirement 29's minimum
    five roles). Superseded at finer grain by Phase 5's ``permission_grant`` — call sites
    go through ``PermissionService.check()``, never this table directly (D11)."""

    __tablename__ = "membership"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principal.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "principal_id", name="uq_membership_tenant_principal"),
        CheckConstraint(
            "role IN ('owner', 'admin', 'editor', 'participant', 'viewer')",
            name="ck_membership_role",
        ),
    )


class Workspace(Base):
    """Minimal for T0.2. ``vocabulary_overlay_id`` and ``default_process_definition_id``
    (plan §12.2) are added by ALTER TABLE migrations once those tables exist (D1.6, B1.1) —
    the same incremental-schema-growth pattern T0.8 uses for ``session``."""

    __tablename__ = "workspace"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    settings: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    # D1.6 (plan §12.2): per-workspace vocabulary overlay override. NULL = fall back to
    # the tenant default (tenant.settings) then the system default (core.vocabulary.
    # service's fallback chain) -- see core/vocabulary/models.py's module docstring for
    # why this is a plain string-referenced FK (no inline import of that module here:
    # core.vocabulary.models itself imports Base from this module, so importing it back
    # from here would be circular; core.tenancy.seed carries the registration-order
    # guard instead, matching every consumer of seed_dev_tenant needing a real Workspace
    # row anyway).
    vocabulary_overlay_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vocabulary_overlay.id", ondelete="SET NULL"),
        nullable=True,
    )
    # G4.2 (plan §12.5, req 21): the workspace's own timeline, advanced only by a
    # deliberate `core.entities.schedule.advance_clock` call. Explicitly NOT wall-clock --
    # process/fictional time and real time are different things, and a value that moved
    # itself on read would make "what changed between sessions" unanswerable. The unit is
    # whatever the workspace's overlay says it is (a day, a sprint, a review cycle); core
    # only knows it is a monotonically-advanced integer.
    clock_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clock_advanced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_workspace_tenant_key"),)


class WorkspaceMembership(Base):
    """Workspace-level role: steward|facilitator|participant|overseer|viewer.

    ``steward`` is the solo creator's combined facilitator+overseer seat (see
    core.tenancy.roles); the other four keep their distinct meanings for multi-human
    workspaces."""

    __tablename__ = "workspace_membership"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principal.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("workspace_id", "principal_id", name="uq_workspace_membership"),
        CheckConstraint(
            "role IN ('steward', 'facilitator', 'participant', 'overseer', 'viewer')",
            name="ck_workspace_membership_role",
        ),
    )


class RolePermission(Base):
    """v1 implementation of ``PermissionService`` (T0.3): data, not code. Global — not
    tenant-scoped, not RLS-covered. Phase 5 adds ``permission_grant(principal, action,
    resource_id)`` alongside this for fine-grained RBAC; call sites (``PermissionService
    .check(principal, action, resource)``) never change (D11)."""

    __tablename__ = "role_permission"

    id: Mapped[uuid.UUID] = _uuid_pk()
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        UniqueConstraint("role", "action", "resource_type", name="uq_role_permission"),
    )
