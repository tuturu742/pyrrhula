"""``agent`` and ``agent`` (plan §12.4). T0.8 built the minimal subset needed to
call a model through the ``ModelProvider`` port; B1.7 (the full agent runtime -- tool loop,
retries, fallback profile) extends this schema in place rather than replacing it, adding
``agent.persona_type`` and ``agent.fallback_agent_id``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base


class ProviderCredentialRow(Base):
    """D1.5: tenant-scoped storage for a provider API key, encrypted at rest through the
    injected ``Encryptor`` port (v1: identity -- real KMS/BYOK is H5.7). Never read back
    through any API route (CLAUDE.md: "the UI never redisplays a key") -- only
    ``agent.credential_ref`` (this row's id, a string) is ever returned to a
    caller. Mutable, not append-only: rotating a key updates the row in place rather than
    leaving old ciphertexts around to be a second thing that could leak.
    """

    __tablename__ = "provider_credential"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    ciphertext: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_provider_credential_tenant", "tenant_id"),)


class Agent(Base):
    __tablename__ = "agent"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # 'ollama'|'echo'|...
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    params: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    # Pointer into a secret manager. NEVER the key itself (CLAUDE.md rule).
    credential_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Per-profile provider endpoint override (e.g. a specific Ollama host:port) --
    # connection setup lives with the profile it belongs to, not a process-wide env var,
    # so a tenant can point different profiles at different local deployments.
    # ``None`` means "use the provider's own default".
    api_base: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Soft-delete: NULL = live, a timestamp = archived (hidden from lists, never hard-deleted
    # via the app role -- see migration c4f2a7e1b9d3). Purge is the superuser CLI's job.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # B1.7: which profile to fall back to after N consecutive provider failures. No
    # inline ForeignKey -- self-referential FKs to the same table are fine to declare
    # inline in SQLAlchemy (no circular CREATE TABLE issue, unlike the cross-table cases
    # elsewhere in this phase), but the *migration* still adds it as a separate
    # ALTER TABLE for symmetry with how this column is actually created.
    fallback_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Persona(Base):
    __tablename__ = "persona"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    # Agents ARE principals (plan §12.4) -- this points at the principal row created
    # alongside the agent, so permission checks and audit actors never need a special case.
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principal.id", ondelete="CASCADE"), nullable=False
    )
    # Per-persona generation overrides, merged OVER the connection's params (an
    # explicit request value still wins over both). This is what keeps five suspects
    # sharing one connection from converging into one voice: each can carry its own
    # temperature, seed, or penalty without anyone cloning connections.
    params: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.id"), nullable=False
    )
    # B1.7 (requirement 8): the column B1.3/B1.6 both found missing and deferred here --
    # the scheduler/eligibility work landing before this task had nothing to query.
    # Informational agents get no persona-persistence expectations (no requirement that
    # their "voice" stay consistent turn to turn the way a facilitator/participant's
    # would) -- a semantic distinction other modules (the future assembler, C1.2) read
    # this field to apply, not something this column enforces itself.
    persona_type: Mapped[str] = mapped_column(String(16), nullable=False, default="participant")
    # D1.5 (plan §12.4): prose persona shown to the model as part of its own turn's
    # context -- not yet wired into core.assembler.layout's rendering (the same
    # "not yet integrated into the runtime" boundary as C1.2-C1.7; the column is real
    # and editable today, the render-time wiring is a separate task).
    persona_md: Mapped[str] = mapped_column(String, nullable=False, default="")
    # G4.8: a general per-agent settings bag (same shape as workspace.settings /
    # tenant.settings). First writer is the CCv3 importer, which must carry the card's
    # `extensions` payload verbatim -- data Pyrrhula does not interpret and must not give
    # a schema to. A general bag, not a card-shaped column: the next thing needing
    # per-agent config shouldn't need another migration.
    settings: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    # Optional link to the agent's Entity (character sheet). No FK: there is no
    # entity/entity_schema table anywhere in Phase 1 (a gap B1.3/B1.4 already found and
    # documented) -- same no-FK-yet shape as ResolutionRecordRow.actor_entity_id, ready
    # for F3.6 (Phase 3) to give this a real target.
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Per-actor internet-search switch: this persona's generate turns get the `web_search`
    # tool only when true AND the workspace's `web_search` MCP server (the egress control)
    # is on the allowlist. Migration f4a7c2e9b6d1.
    web_search: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Soft-delete (migration c4f2a7e1b9d3): NULL = live, a timestamp = archived. An archived
    # agent is filtered from list_personas AND from the scheduler's candidate resolver, so it
    # never gets a turn again -- but its append-only history stays intact.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "persona_type IN ('supervisor', 'participant', 'informational')",
            name="ck_persona_persona_type",
        ),
    )


class PersonaVersion(Base):
    """F3.12: an append-only history log of ``agent.persona_md`` over time -- unlike
    knowledge, ``persona_md`` itself stays a plain mutable column (no draft/published
    split exists for it), so this table is a log an edit writes *alongside* that column
    update, not the column's own source of truth. Written by
    ``core.agents.editing.apply_persona_edit_proposal`` on approval, and by nothing else
    -- a manual edit through ``update_persona`` does not currently log a version here (a
    real, documented gap; see F3.12's scope note)."""

    __tablename__ = "persona_version"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    persona_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.id", ondelete="CASCADE"), nullable=False
    )
    persona_md: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL"), nullable=True
    )
    ai_assisted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
