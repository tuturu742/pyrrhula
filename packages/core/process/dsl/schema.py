"""ProcessDefinition DSL (plan §5.2, D1, B1.1): the declarative, versioned JSON document
that drives the process interpreter (B1.2). Restricted vocabulary; no expressions except
CEL; every phase statically validated on save so the interpreter (B1.2) can trust its
input completely — a definition that passes ``validate_definition`` (validator.py) is
never allowed to make the interpreter fault on structural grounds.

The plan's §5.2 YAML block is illustrative prose, not literal, parseable syntax — it uses
``-> target_phase`` as informal transition shorthand and a gate entry shaped like
``{ on: actor_declares_action, -> action_phase }``, which isn't valid YAML/JSON (a mapping
entry needs a ``key: value`` shape; a bare ``-> action_phase`` isn't one). This module
defines a concrete, unambiguous, round-trippable JSON representation of the *same semantic
content*: transitions are always an explicit ``to:`` field, matching how B1.1's own
acceptance criterion ("parse -> validate -> serialize identically") requires something
that is actually re-parseable.

``id``/``version``/``key`` are deliberately *not* fields on this model — those are
``process_definition`` row/versioning metadata (assigned by the authoring/publish layer),
not part of the authored document's own content, mirroring A1.1's KnowledgeSource-vs-
KnowledgeSourceVersion split between identity/versioning and content.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Agents ARE principals with one of these three roles (plan §12.4, requirement 8).
KNOWN_PERSONA_TYPES = frozenset({"supervisor", "participant"})

# The vocabulary an actors[].any_of entry may draw from: a persona-type-based token
# ("<type>_agent", for any KNOWN_PERSONA_TYPES member) or a human-based token. Kept as an
# explicit closed set (not "any persona_type + '_agent'") so a typo'd type in any_of is a
# validation error, not a silently-never-matching string at runtime.
KNOWN_ANY_OF_TOKENS = frozenset(f"{role}_agent" for role in KNOWN_PERSONA_TYPES) | {
    "human_participant",
    "human_overseer",
}

_DURATION_RE = re.compile(r"^(\d+)(h|m|s|d)$")


def parse_duration_seconds(duration: str) -> int:
    """``"24h"`` -> 86400. The only duration syntax the DSL accepts (await timeouts,
    ``on: timeout(...)`` gates) — deliberately not a general date/time parser."""
    match = _DURATION_RE.match(duration)
    if match is None:
        raise ValueError(
            f"invalid duration {duration!r}: expected '<int><h|m|s|d>', e.g. '24h', '72h', '30m'"
        )
    value, unit = int(match.group(1)), match.group(2)
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return value * multiplier


_TIMEOUT_EVENT_RE = re.compile(r"^timeout\((\d+[hmsd])\)$")


class StateVarSpec(BaseModel):
    """One entry of the DSL's ``state:`` block — a session-scoped, typed variable with a
    default applied at session start (B1.2)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["integer", "string", "boolean", "number"]
    default: int | str | bool | float

    @model_validator(mode="after")
    def _default_matches_type(self) -> StateVarSpec:
        checks: dict[str, type | tuple[type, ...]] = {
            "integer": int,
            "number": (int, float),
            "string": str,
            "boolean": bool,
        }
        expected = checks[self.type]
        # bool is a subclass of int in Python -- explicitly reject a bool default for an
        # 'integer'/'number' var and an int default for 'boolean', since CEL and the
        # interpreter treat them as genuinely different types.
        if self.type in ("integer", "number") and isinstance(self.default, bool):
            raise ValueError(f"default {self.default!r} is a bool, not a {self.type}")
        if self.type == "boolean" and not isinstance(self.default, bool):
            raise ValueError(f"default {self.default!r} is not a boolean")
        if not isinstance(self.default, expected):
            raise ValueError(f"default {self.default!r} does not match declared type {self.type!r}")
        return self


class ActorSpec(BaseModel):
    """One entry of a phase's ``actors:`` list -- one "who may act" clause. Exactly one of
    ``persona_type``/``any_of``/``human_participant`` selects eligible actors; ``mode``
    decides whether an eligible actor's turn is human-typed (``free``), model-generated
    (``generate``), or human-typed-in-an-agent's-place (``generate_as``, requirement 10)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    persona_type: str | None = None
    any_of: list[str] | None = None
    human_participant: Literal["all"] | None = None
    mode: Literal["free", "generate", "generate_as"]
    # "addressed": like declared, but whoever the previous speaker named goes first --
    # what lets a facilitator's "Marta, where were you?" actually reach Marta before the
    # roster walk hands the floor to whoever was declared first.
    # "reactive": whoever the last message names goes next; nobody named, a
    # deterministic pick weighted by each persona's chattiness axis; the previous
    # speaker never follows themselves. The floor follows the conversation instead of
    # a roster.
    order: Literal["declared", "initiative", "free", "addressed", "reactive"] = "declared"
    from_field: str | None = Field(default=None, alias="from")
    max_turns: int | None = None

    @model_validator(mode="after")
    def _selector_shape(self) -> ActorSpec:
        """At most one of persona_type/any_of/human_participant selects eligible actors by
        role. If none is set, ``order: initiative`` must be -- eligibility is then
        implicit: whoever has the referenced entity field (plan §5.2's own example omits
        an explicit selector on its initiative-ordered actor entry, relying on exactly
        this). Any other combination -- two selectors, or no selector with a non-
        initiative order -- is ambiguous and rejected."""
        selectors = [
            self.persona_type is not None,
            self.any_of is not None,
            self.human_participant is not None,
        ]
        selector_count = sum(selectors)
        if selector_count > 1:
            raise ValueError(
                "at most one of persona_type, any_of, human_participant may be set "
                "on an actor entry"
            )
        if selector_count == 0 and self.order != "initiative":
            raise ValueError(
                "an actor entry needs one of persona_type, any_of, human_participant, unless "
                "order is 'initiative' (eligibility is then implicit: whoever has the field)"
            )
        return self

    @model_validator(mode="after")
    def _initiative_requires_from(self) -> ActorSpec:
        if self.order == "initiative" and not self.from_field:
            raise ValueError("order: initiative requires a 'from' field (e.g. entity_field(\"x\"))")
        return self

    @field_validator("persona_type")
    @classmethod
    def _known_agent_role(cls, value: str | None) -> str | None:
        if value is not None and value not in KNOWN_PERSONA_TYPES:
            raise ValueError(
                f"unknown persona_type {value!r}; known: {sorted(KNOWN_PERSONA_TYPES)}"
            )
        return value

    @field_validator("any_of")
    @classmethod
    def _known_any_of_tokens(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            unknown = [token for token in value if token not in KNOWN_ANY_OF_TOKENS]
            if unknown:
                raise ValueError(
                    f"unknown any_of token(s) {unknown!r}; known: {sorted(KNOWN_ANY_OF_TOKENS)}"
                )
        return value


class VisibilitySpec(BaseModel):
    """§4.1/§6.3's "who sees what" declared per-phase -- mandatory, no default (requirement
    13 made structural). ``secrets`` values are provisional pending C1.1/Phase 2's full
    disclosure-state machine; only the vocabulary the plan's own example uses is accepted
    today, deliberately narrow rather than a permissive free string."""

    model_config = ConfigDict(extra="forbid")

    knowledge_classes: list[str]
    scopes: list[str]
    entity_fields: Literal["all"] | list[str]
    secrets: Literal["held_by_actor", "none"]


class BudgetSpec(BaseModel):
    """Per-phase token budget (D2, A1.6) -- this is where rule-vs-lore priority actually
    lives. ``spill`` mirrors A1.6's ``budget.py`` policy names directly.

    ``history_ratio`` (G4.1) is the *reservation* elapsed-history repopulation takes out
    of ``max_tokens`` **before** retrieval runs, not a truncation applied to whatever
    retrieval already spent: ``search_and_budget`` is handed
    ``max_tokens - int(max_tokens * history_ratio)``, so a resumed session's recap can
    never be squeezed out by a greedy retrieval pass that ran first. It is deliberately
    *not* an entry in ``ratio`` (which sums to ~1.0 across knowledge *classes* -- history
    is not a knowledge class, and folding it in there would make every pack's class
    ratios mean something different depending on whether the phase resumes).

    Default ``0.0`` = no reservation, which is exactly the pre-G4.1 behaviour for every
    already-authored phase.
    """

    model_config = ConfigDict(extra="forbid")

    ratio: dict[str, float]
    max_tokens: int
    spill: Literal["proportional", "none"] = "proportional"
    history_ratio: float = 0.0

    @field_validator("history_ratio")
    @classmethod
    def _history_ratio_in_range(cls, value: float) -> float:
        if not 0.0 <= value < 1.0:
            raise ValueError(
                f"history_ratio {value!r} out of range: expected 0.0 <= history_ratio < 1.0 "
                "(1.0 would reserve the entire phase budget and leave retrieval nothing)"
            )
        return value


class GateSpec(BaseModel):
    """A phase's ``gates:`` entry, evaluated once the scheduler has no next actor.
    Exactly one of ``on``/``when``/``else_`` selects when this gate fires; declaration
    order is the evaluation order (first match wins), matching B1.2's interpreter loop."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    on: str | None = None
    when: str | None = None
    else_: bool | None = Field(default=None, alias="else")
    to: str

    @model_validator(mode="after")
    def _exactly_one_condition(self) -> GateSpec:
        conditions = [self.on is not None, self.when is not None, self.else_ is True]
        if sum(conditions) != 1:
            raise ValueError("exactly one of on, when, else must be set on a gate entry")
        return self

    @field_validator("on")
    @classmethod
    def _known_gate_event(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value == "actor_declares_action":
            return value
        match = _TIMEOUT_EVENT_RE.match(value)
        if match is None:
            raise ValueError(
                f"unknown gate event {value!r}; expected 'actor_declares_action' or "
                f"'timeout(<duration>)' e.g. 'timeout(24h)'"
            )
        parse_duration_seconds(match.group(1))  # raises on malformed duration
        return value


class EffectSpec(BaseModel):
    """A phase's ``effects:`` entry, applied atomically with its transition (B1.2):
    ``state.<set>`` is assigned the result of evaluating the ``to`` CEL expression."""

    model_config = ConfigDict(extra="forbid")

    set: str
    to: str


class AwaitSpec(BaseModel):
    """The interrupt primitive (§5.2, B1.6): a phase suspends for human input (or, later,
    an external result) until satisfied or ``timeout`` elapses, at which point
    ``on_timeout`` is the transition target.

    ``reminder_at`` (G4.3) is pacing data, not new engine semantics: the elapsed duration
    after which a human who hasn't acted gets one nudge. B1.6's timeout sweep reads it;
    the interpreter never sees it. ``None`` inherits the definition-level
    ``pacing.reminder_at``, and if that is absent too, no reminder is sent -- silence is
    the correct default for a process whose author never asked for one."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["human_input"]
    timeout: str
    on_timeout: str
    reminder_at: str | None = None

    @field_validator("timeout")
    @classmethod
    def _valid_duration(cls, value: str) -> str:
        parse_duration_seconds(value)
        return value

    @field_validator("reminder_at")
    @classmethod
    def _valid_reminder_duration(cls, value: str | None) -> str | None:
        if value is not None:
            parse_duration_seconds(value)
        return value

    @model_validator(mode="after")
    def _reminder_precedes_timeout(self) -> AwaitSpec:
        if self.reminder_at is None:
            return self
        if parse_duration_seconds(self.reminder_at) >= parse_duration_seconds(self.timeout):
            raise ValueError(
                f"reminder_at {self.reminder_at!r} must be shorter than timeout "
                f"{self.timeout!r} -- a reminder that fires at or after the deadline is a "
                "reminder nobody can act on"
            )
        return self


class PacingSpec(BaseModel):
    """Definition-level pacing defaults (G4.3, req 21): "each actor has 48h, reminder at
    24h". Data on the ProcessDefinition, inherited by any ``await`` that doesn't state its
    own -- a play-by-post game and a week-long enterprise review cycle differ in these two
    numbers and in nothing else, which is exactly why they belong in the definition rather
    than in the engine."""

    model_config = ConfigDict(extra="forbid")

    reminder_at: str

    @field_validator("reminder_at")
    @classmethod
    def _valid_duration(cls, value: str) -> str:
        parse_duration_seconds(value)
        return value


class PhaseSpec(BaseModel):
    """One phase of a ProcessDefinition. ``budget`` is optional -- a pure-await phase like
    the plan's ``feedback_loop`` example generates no agent turn and needs no context
    budget. ``on_complete`` and ``gates`` are both optional transition mechanisms: a phase
    with no ``gates`` and a set ``on_complete`` transitions there unconditionally once
    actors are exhausted; a phase with ``gates`` evaluates them in order instead. Having
    both set is a validation error (validator.py) -- one phase, one transition mechanism,
    not an ambiguous "which wins" question left to interpreter behaviour.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    label_key: str
    actors: list[ActorSpec]
    visibility: VisibilitySpec
    # Task instructions injected into every acting persona's turn context for this phase
    # (a system block alongside persona + agenda). Empty = the pre-existing behavior: the
    # model gets no phase-specific instruction. Declarative prose, not code — the place a
    # flow author says "output ## headings X/Y/Z, no filler".
    prompt: str = ""
    budget: BudgetSpec | None = None
    gates: list[GateSpec] = Field(default_factory=list)
    effects: list[EffectSpec] = Field(default_factory=list)
    await_field: AwaitSpec | None = Field(default=None, alias="await")
    flags: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    # Which registered remote MCP tools the ACTING persona may use in this phase, by
    # tool name. None (the default) keeps the legacy behaviour -- every workspace
    # remote tool is offered; [] offers none; a list is an allowlist. This is what lets
    # a flow hand an oracle to its facilitator alone: put the tool on the phases only
    # the facilitator acts in, and [] on everyone else's.
    remote_tools: list[str] | None = None
    on_complete: str | None = None

    def history_slice_tokens(self) -> int:
        """G4.1: the phase budget's declared history reservation, in tokens. ``0`` for a
        phase with no budget or no ``history_ratio`` -- the pre-G4.1 default, so every
        already-authored phase keeps its exact previous retrieval budget."""
        if self.budget is None:
            return 0
        return int(self.budget.max_tokens * self.budget.history_ratio)


class ProcessDefinitionDSL(BaseModel):
    """The authored document. ``id``/``version``/``key`` live on the ``process_definition``
    row, not here -- see module docstring."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    vocabulary_overlay: str
    state: dict[str, StateVarSpec] = Field(default_factory=dict)
    initial_phase: str
    phases: dict[str, PhaseSpec]
    pacing: PacingSpec | None = None

    def reminder_duration_for(self, phase: PhaseSpec) -> str | None:
        """The reminder duration in effect for one phase's await: its own, else the
        definition's default, else none. One resolution function so the sweep and the UI
        can never disagree about which value applies."""
        if phase.await_field is None:
            return None
        if phase.await_field.reminder_at is not None:
            return phase.await_field.reminder_at
        return self.pacing.reminder_at if self.pacing is not None else None
