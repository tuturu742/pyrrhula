"""Minimal ``session``/``session_event``/``message`` for the walking
skeleton: a hardcoded 2-phase process (``prompt`` -> ``respond``). The interpreter,
phase engine, checkpoints and await/resume machinery all extend this schema in place; it
is the forward-compatible subset, not a throwaway.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

# MessageRow.context_manifest_id FKs to context_manifest.id by string reference --
# SQLAlchemy only resolves that at mapper-configuration time, which requires
# ContextManifestRow's module to have been imported by *someone* first. Importing it here
# guarantees that regardless of what a caller of this module imports.
import core.assembler.models  # noqa: E402, F401

# same reasoning, for SessionRow.process_definition_id -> process_definition.id.
import core.process.models  # noqa: E402, F401
from core.tenancy.models import Base


class SessionRow(Base):
    __tablename__ = "session"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    persona_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent.id", ondelete="CASCADE"), nullable=False
    )
    # the hardcoded two-phase process used 'prompt'/'respond' (fit in 16 chars); the
    # real interpreter runs an arbitrary DSL phase graph, so this is widened to match
    # process_definition phase-key lengths.
    current_phase: Mapped[str] = mapped_column(String(63), nullable=False, default="prompt")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # What the last advance said this session is waiting for: "human" when the next actor
    # is free-mode and nobody has submitted, NULL otherwise. `status` cannot carry this --
    # an awaiting session is genuinely still active, not paused -- but without it the UI
    # renders "your move" and "being worked on" identically.
    awaiting: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Soft-delete (migration c4f2a7e1b9d3): NULL = live, a timestamp = archived (hidden from
    # session lists). Distinct from `status` (active/paused) -- an archived session is
    # removed from view, not paused; its messages/manifests/resolutions stay append-only.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # #5: free-form agenda. Injected into the supervisor persona's turn context so it steers
    # the discussion/flow along it. Context, not an enforced state machine.
    agenda_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Human-given display label (migration c7d2f9e4a1b8). Nullable: unnamed sessions fall
    # back to an agenda snippet / creation date in the UI.
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # #7: how discussion turns are driven -- 'auto' (the autonomous scheduler runs
    # participant turns to completion) or 'directed' (a human overseer conducts each turn;
    # the loop parks at the conductable phase). Mutable, toggled live -- the interpreter is
    # gated on this at runtime (core.process.live_session), so one pinned definition serves
    # both modes. Migration b7f4e2a1c9d3.
    turn_policy: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'auto'"), default="auto"
    )
    next_event_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # the DSL's state:-declared session variables. NOT NULL, defaults to '{}' --
    # the hardcoded skeleton sessions never touch this and keep working unmodified.
    state: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    # Which definition (and immutable version of it) this session is running -- pinned at
    # session start, nullable because skeleton sessions have none.
    process_definition_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("process_definition.id", ondelete="SET NULL"), nullable=True
    )
    process_definition_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # the turn scheduler's persisted position within the current phase's actor
    # rotation (core.process.scheduler) -- survives kill/resume without skipping or
    # double-acting an actor. NOT NULL, defaults to '{}' (= "no cursor yet").
    actor_cursor: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    # a fork is a new session rooted at a specific checkpoint. Nullable -- most
    # sessions are never forked. No inline ForeignKey: session and checkpoint FK to each
    # other (a checkpoint FKs to its session; the session's fork-origin pointer FKs to a
    # checkpoint) -- use_alter (in __table_args__ below) defers this one to a separate
    # ALTER TABLE instead of an unresolvable circular CREATE TABLE dependency, the same
    # fix knowledge_source/knowledge_source_version uses for its identical shape.
    forked_from_checkpoint_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # optimistic lock, incremented on every committed advance (core.process.locking).
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # the watchdog-aware claim marker -- a real SELECT ... FOR UPDATE is held only
    # for the brief claim/commit critical sections, never across a slow external model
    # call, so these two columns (not a DB lock) are what a crash mid-call leaves behind
    # for a timeout-aware re-claim to recognise as abandoned.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # HMAC key for this session's seeded randomizer results -- generated
    # lazily on first resolution, not at session creation, so every pre-existing session
    # (and every session that never rolls a randomizer call) never needs one. Server-side only until
    # deliberately revealed to players post-session for roll verification -- revealing it
    # is a UI/API decision outside this column's own concern.
    roll_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["forked_from_checkpoint_id"],
            ["checkpoint.id"],
            ondelete="SET NULL",
            use_alter=True,
            name="fk_session_forked_from_checkpoint",
        ),
    )


class SessionPersonaRow(Base):
    """#4: the explicit roster of a session -- exactly one supervisor persona and one or more
    participant personas, chosen at creation. The scheduler resolves a phase's actors from
    this set (by persona_type), so only deliberately-selected personas ever act."""

    __tablename__ = "session_persona"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    persona_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("persona.id", ondelete="CASCADE"), nullable=False
    )
    is_supervisor: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("session_id", "persona_id", name="uq_session_persona"),)


class SessionEventRow(Base):
    __tablename__ = "session_event"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # Event kind: message, phase_transition, phase_requirement, error, ...
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    actor_principal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("session_id", "event_seq", name="uq_session_event_seq"),)


class MessageRow(Base):
    __tablename__ = "message"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    author_principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The message role as the model saw it.
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # 'user'|'assistant'
    content_md: Mapped[str] = mapped_column(String, nullable=False)
    # FK to core.assembler.models.ContextManifestRow. ondelete=SET NULL: a
    # manifest is append-only in practice (no UPDATE/DELETE grant) so this practically
    # never fires, but a message should never become unreadable if it somehow did.
    context_manifest_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("context_manifest.id", ondelete="SET NULL"),
        nullable=True,
    )
    # {"contradiction": ["<resolution_record id>", ...]} when the contradiction
    # scanner flags this reply against one or more of its turn's resolution records --
    # empty dict otherwise. Also carries moderation signals (content-filter flags etc.).
    moderation_flags: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    # the validated citation set -- [{citation_id, entry_key, source_id,
    # version_id}, ...], version-pinned at generation time. Populated
    # by core.assembler.citations.apply_citation_validation, never the raw cited ids the
    # reply text mentions (those might include hallucinated ones -- see bad_citation in
    # moderation_flags for that signal).
    citations: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list)
    # every ResolutionRecord produced by a tool call during this turn --
    # written by core.agents.runtime._commit_turn regardless of whether a contradiction
    # was found, so the session view's resolution widget (INV-7) can render a turn's
    # mechanical results straight from the record even when the narration matched perfectly.
    # `moderation_flags["contradiction"]` is the subset of these ids the narration
    # actually conflicted with, not a separate id space.
    resolution_record_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # a human replied in place of an agent.
    # ``author_principal_id`` deliberately stays the *agent's* principal -- the scheduler
    # and the transcript must treat this as that agent's turn, or it isn't an override,
    # it's a different actor speaking out of order. ``overridden_by_principal_id`` is
    # where the human is recorded, and it is never NULL when ``was_human_override``.
    was_human_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rewrite_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The human's own words, kept when a voice-conformance rewrite was applied -- an
    # overseer reviewing an override must be able to see what was actually typed, not
    # only what the model polished it into.
    original_content_md: Mapped[str | None] = mapped_column(String, nullable=True)
    overridden_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CheckpointRow(Base):
    """Append-only: a snapshot of ``{phase, state, actor_cursor,
    entity_versions, knowledge_version_pins}`` written at every phase transition. The app
    role has no UPDATE/DELETE grant (migration 5e2c9b4f8a13) -- a checkpoint is a
    historical fact, never edited after the fact, same shape as
    ``knowledge_source_version``/``audit_log``.

    ``entity_versions`` is always ``{}`` today: there is no ``entity``/``entity_schema``
    table when this schema was first written --
    the column exists so a later phase's entity work extends this schema in place rather
    than adding a new one.
    """

    __tablename__ = "checkpoint"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(63), nullable=False)
    state: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    actor_cursor: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    entity_versions: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    knowledge_version_pins: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("session_id", "event_seq", name="uq_checkpoint_session_event_seq"),
        Index("ix_checkpoint_session_created", "session_id", "created_at"),
    )


class AwaitStateRow(Base):
    """The interrupt primitive (``await``). ``tenant_id`` is added beyond the original
    schema sketch -- see the migration's docstring for
    why. ``outcome`` is ``NULL`` while pending, ``'satisfied'`` or ``'timed_out'`` once
    resolved -- exactly one of ``core.process.awaits.satisfy_await``/``resolve_timeout``
    can ever win the atomic ``UPDATE ... WHERE outcome IS NULL`` that sets it.
    """

    __tablename__ = "await_state"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    await_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Descriptive record of who was eligible to satisfy this (the phase's actors at the
    # time the await was created) -- not itself an authorization check; see
    # core.process.awaits module docstring for the scope boundary this implies.
    expected_from: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    timeout_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    on_timeout_phase: Mapped[str] = mapped_column(String(63), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    satisfied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # when this await's one nudge is due, resolved at creation from the phase's own
    # ``reminder_at`` or the definition's ``pacing.reminder_at``. NULL = no reminder was
    # configured (the default for awaits that predate reminders, and for every process
    # whose author never asked for one).
    reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index(
            "ix_await_state_pending_timeout",
            "timeout_at",
            postgresql_where=text("outcome IS NULL"),
        ),
        Index(
            "ix_await_state_pending_reminder",
            "reminder_at",
            postgresql_where=text("outcome IS NULL AND reminder_at IS NOT NULL"),
        ),
        Index("ix_await_state_session", "session_id"),
    )
