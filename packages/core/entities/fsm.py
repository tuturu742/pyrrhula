"""Declarative FSMs on entity schemas (F3.2, plan §10.3, §12.5): states with tags and
enter/exit effects, transitions with CEL guards and triggers. The req-18 bet in one
definition shape: ``healthy -> bloodied -> unconscious -> dead`` and
``draft -> submitted -> in_review -> approved -> archived`` are the same structure --
this module never branches on which one it's looking at.

**Effects are declarative because CLAUDE.md rule 10 is absolute.** ``invoke_tool`` names
a registered deterministic tool; it does not carry code. Anything a pack "needs" beyond
this vocabulary (``set_field``, ``emit_event``, ``invoke_tool``, ``apply_modifier``,
``transition_other``) is a missing core abstraction to raise, not to work around
(rule 9).

``ANY_STATE`` (``"*"``) lets a transition's ``from`` match every declared state --
needed for the "reachable from any active state" shape F3.13's swdev ``work_item``
schema uses (``blocked``), built here rather than patched in later once a real pack
needs it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

# EntityStateChangeRow FKs to session.id by string reference -- SQLAlchemy only resolves
# that at mapper-configuration time, which requires core.sessions.models to have been
# imported by *someone* first (the same registration-order fix core.resolution.records
# needed for its own session.id/rule_system.id FKs). Importing it here guarantees that
# regardless of what a caller of this module imports.
import core.sessions.models  # noqa: E402, F401
from core.entities.cel import CELValidationError, compile_check, evaluate
from core.tenancy.models import Base

ANY_STATE = "*"

EffectKind = Literal["set_field", "emit_event", "invoke_tool", "apply_modifier", "transition_other"]

_REQUIRED_FIELDS_BY_KIND: dict[EffectKind, tuple[str, ...]] = {
    "set_field": ("field", "value"),
    "apply_modifier": ("field", "value"),
    "emit_event": ("event",),
    "invoke_tool": ("tool_key",),
    "transition_other": ("machine", "trigger"),
}


class EffectDef(BaseModel):
    """One declarative effect. Every field beyond ``kind`` is optional at the type
    level and validated as required-per-kind below, so the model stays one flat,
    ``extra=forbid`` shape rather than a discriminated union with five near-identical
    variants."""

    model_config = ConfigDict(extra="forbid")

    kind: EffectKind
    field: str | None = None
    value: str | None = None  # CEL expression (set_field/apply_modifier)
    event: str | None = None
    tool_key: str | None = None
    tool_args: dict[str, object] = {}
    machine: str | None = None
    trigger: str | None = None

    @model_validator(mode="after")
    def _required_for_kind(self) -> EffectDef:
        for name in _REQUIRED_FIELDS_BY_KIND[self.kind]:
            if getattr(self, name) is None:
                raise ValueError(f"effect kind {self.kind!r} requires {name!r}")
        return self


class StateDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label_key: str
    tags: list[str] = []
    on_enter: list[EffectDef] = []
    on_exit: list[EffectDef] = []


class TransitionDef(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_state: str = Field(alias="from")
    to: str
    trigger: str
    guard: str | None = None
    effects: list[EffectDef] = []


class StateMachineDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    states: list[StateDef]
    initial: str
    transitions: list[TransitionDef] = []

    @model_validator(mode="after")
    def _initial_is_declared(self) -> StateMachineDef:
        keys = {s.key for s in self.states}
        if self.initial not in keys:
            raise ValueError(f"machine {self.key!r}: initial state {self.initial!r} not declared")
        return self


@dataclass(frozen=True)
class FSMValidationIssue:
    field_path: str
    message: str


def _validate_effect(
    path: str, effect: EffectDef, field_types: Mapping[str, str], machine_keys: set[str]
) -> list[FSMValidationIssue]:
    issues: list[FSMValidationIssue] = []
    if effect.kind in ("set_field", "apply_modifier"):
        assert effect.field is not None and effect.value is not None
        if effect.field not in field_types:
            issues.append(
                FSMValidationIssue(
                    f"{path}.field", f"effect references undeclared field {effect.field!r}"
                )
            )
        else:
            try:
                compile_check(effect.value, dict(field_types))
            except CELValidationError as exc:
                issues.append(FSMValidationIssue(f"{path}.value", str(exc)))
    if effect.kind == "transition_other":
        assert effect.machine is not None
        if effect.machine not in machine_keys:
            issues.append(
                FSMValidationIssue(
                    f"{path}.machine", f"effect references undeclared machine {effect.machine!r}"
                )
            )
    return issues


def _check_reachability(
    path_prefix: str, machine: StateMachineDef, state_keys: set[str]
) -> list[FSMValidationIssue]:
    edges: dict[str, set[str]] = {k: set() for k in state_keys}
    for transition in machine.transitions:
        if transition.from_state == ANY_STATE:
            for k in state_keys:
                edges[k].add(transition.to)
        elif transition.from_state in edges:
            edges[transition.from_state].add(transition.to)

    reachable: set[str] = set()
    frontier = [machine.initial]
    while frontier:
        current = frontier.pop()
        if current in reachable:
            continue
        reachable.add(current)
        frontier.extend(edges.get(current, set()) - reachable)

    unreachable = state_keys - reachable
    return [
        FSMValidationIssue(f"{path_prefix}.states", f"state {key!r} is unreachable from initial")
        for key in sorted(unreachable)
    ]


def validate_state_machines(
    field_types: Mapping[str, str], machines: list[StateMachineDef]
) -> list[FSMValidationIssue]:
    """Static validation (F3.2): reachability from ``initial``, no transition to/from an
    undeclared state (``ANY_STATE`` excepted for ``from``), guards compile, effects
    reference real fields/machines -- reuses F3.1's CEL compile-check and validator
    patterns (``core.process.dsl.validator``'s reachability shape)."""
    issues: list[FSMValidationIssue] = []
    machine_keys = {m.key for m in machines}

    for m_idx, machine in enumerate(machines):
        state_keys = {s.key for s in machine.states}
        path_prefix = f"state_machines[{m_idx}]"

        for t_idx, transition in enumerate(machine.transitions):
            t_path = f"{path_prefix}.transitions[{t_idx}]"
            if transition.from_state != ANY_STATE and transition.from_state not in state_keys:
                issues.append(
                    FSMValidationIssue(
                        f"{t_path}.from",
                        f"transition source {transition.from_state!r} is not a declared state",
                    )
                )
            if transition.to not in state_keys:
                issues.append(
                    FSMValidationIssue(
                        f"{t_path}.to",
                        f"transition target {transition.to!r} is not a declared state",
                    )
                )
            if transition.guard is not None:
                try:
                    compile_check(transition.guard, dict(field_types))
                except CELValidationError as exc:
                    issues.append(FSMValidationIssue(f"{t_path}.guard", str(exc)))
            for e_idx, effect in enumerate(transition.effects):
                issues.extend(
                    _validate_effect(
                        f"{t_path}.effects[{e_idx}]", effect, field_types, machine_keys
                    )
                )

        for s_idx, state in enumerate(machine.states):
            for phase, effects in (("on_enter", state.on_enter), ("on_exit", state.on_exit)):
                for e_idx, effect in enumerate(effects):
                    issues.extend(
                        _validate_effect(
                            f"{path_prefix}.states[{s_idx}].{phase}[{e_idx}]",
                            effect,
                            field_types,
                            machine_keys,
                        )
                    )

        # Reachability assumes every declared target is real -- skip it for this
        # machine if a dangling target was already found, matching
        # core.process.dsl.validator's identical ordering rationale.
        has_dangling = any(
            issue.field_path.startswith(f"{path_prefix}.transitions")
            and issue.field_path.endswith(".to")
            for issue in issues
        )
        if not has_dangling:
            issues.extend(_check_reachability(path_prefix, machine, state_keys))

    return issues


def eligible_transitions(
    machine: StateMachineDef, current_state: str, trigger: str
) -> list[TransitionDef]:
    return [
        t
        for t in machine.transitions
        if t.trigger == trigger and (t.from_state == current_state or t.from_state == ANY_STATE)
    ]


def guard_passes(transition: TransitionDef, fields: Mapping[str, object]) -> bool:
    if transition.guard is None:
        return True
    return bool(evaluate(transition.guard, dict(fields)))


class EntityStateChangeRow(Base):
    """Append-only (CLAUDE.md rule 5): every state change -- FSM-driven or a plain field
    edit (F3.5's ``mutate()``) -- lands here. ``entity_id`` has no FK yet: the ``entity``
    table doesn't exist until F3.3, which ALTERs this table to add the constraint once
    its target exists (the same incremental-schema-growth pattern
    ``core.tenancy.models.Workspace.vocabulary_overlay_id`` documents for an identical
    forward-reference reason). ``session_id``/``event_seq`` are nullable -- G4.2's future
    out-of-session mutations have neither."""

    __tablename__ = "entity_state_change"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("session.id", ondelete="SET NULL"), nullable=True
    )
    event_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    field_path: Mapped[str] = mapped_column(String(255), nullable=False)
    old_value: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    cause: Mapped[str] = mapped_column(String(16), nullable=False)
    cause_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "cause IN ('tool', 'fsm', 'human', 'agent', 'import')",
            name="ck_entity_state_change_cause",
        ),
    )


async def evaluate_and_record_transition(
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    session_id: uuid.UUID | None,
    event_seq: int | None,
    machine: StateMachineDef,
    current_state: str,
    trigger: str,
    fields: Mapping[str, object],
    *,
    cause: str = "fsm",
    cause_ref: str | None = None,
) -> str | None:
    """The interpreter's transition step, in isolation from F3.5's locking/idempotency
    (that transactional wrapping is F3.5's own job -- this function owns the "does the
    transition fire, and if so what gets recorded" question, callable identically for
    the HP-style and ticket-style fixtures with zero domain branching).

    Returns the new state key and appends exactly one ``entity_state_change`` row if a
    matching transition's guard passes; returns ``None`` and writes nothing if no
    transition matches the trigger from ``current_state``, or a matching transition's
    guard evaluates false.
    """
    from core.tenancy.scope import tenant_scope

    candidates = eligible_transitions(machine, current_state, trigger)
    for transition in candidates:
        if not guard_passes(transition, fields):
            continue
        async with tenant_scope(tenant_id) as session:
            session.add(
                EntityStateChangeRow(
                    tenant_id=tenant_id,
                    entity_id=entity_id,
                    session_id=session_id,
                    event_seq=event_seq,
                    field_path=f"fsm_states.{machine.key}",
                    old_value=current_state,
                    new_value=transition.to,
                    cause=cause,
                    cause_ref=cause_ref,
                )
            )
        return transition.to
    return None
