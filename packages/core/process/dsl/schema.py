"""ProcessDefinition DSL: the declarative, versioned JSON document
that drives the process interpreter. Restricted vocabulary; no expressions except
CEL; every phase statically validated on save so the interpreter can trust its
input completely — a definition that passes ``validate_definition`` (validator.py) is
never allowed to make the interpreter fault on structural grounds.

The YAML block is illustrative prose, not literal, parseable syntax — it uses
``-> target_phase`` as informal transition shorthand and a gate entry shaped like
``{ on: actor_declares_action, -> action_phase }``, which isn't valid YAML/JSON (a mapping
entry needs a ``key: value`` shape; a bare ``-> action_phase`` isn't one). This module
defines a concrete, unambiguous, round-trippable JSON representation of the *same semantic
content*: transitions are always an explicit ``to:`` field, matching how its own
acceptance criterion ("parse -> validate -> serialize identically") requires something
that is actually re-parseable.

``id``/``version``/``key`` are deliberately *not* fields on this model — those are
``process_definition`` row/versioning metadata (assigned by the authoring/publish layer),
not part of the authored document's own content, mirroring the KnowledgeSource-vs-
KnowledgeSourceVersion split between identity/versioning and content.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Agents ARE principals with one of these three roles (requirement 8).
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
    default applied at session start."""

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
    # A CAP on this entry, not a round count. "declared"/"initiative" resolve the
    # roster once and walk it once, so an entry in front of three actors gives three
    # turns whatever this says; "reactive"/"free" re-pick every turn, so there it is
    # the turn count. A phase that wants three rounds writes the entry three times --
    # which is also what lets a supervisor entry sit between them.
    max_turns: int | None = None

    @model_validator(mode="after")
    def _selector_shape(self) -> ActorSpec:
        """At most one of persona_type/any_of/human_participant selects eligible actors by
        role. If none is set, ``order: initiative`` must be -- eligibility is then
        implicit: whoever has the referenced entity field ('s own example omits
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
    """"Who sees what", declared per phase -- mandatory, no default. ``secrets`` values
    name how the disclosure-state machine is consulted; only the documented vocabulary is
    accepted
    today, deliberately narrow rather than a permissive free string."""

    model_config = ConfigDict(extra="forbid")

    knowledge_classes: list[str]
    scopes: list[str]
    entity_fields: Literal["all"] | list[str]
    secrets: Literal["held_by_actor", "none"]


class BudgetSpec(BaseModel):
    """Per-phase token budget -- this is where rule-vs-lore priority actually
    lives. ``spill`` mirrors the ``budget.py`` policy names directly.

    ``history_ratio`` is the *reservation* elapsed-history repopulation takes out
    of ``max_tokens`` **before** retrieval runs, not a truncation applied to whatever
    retrieval already spent: ``search_and_budget`` is handed
    ``max_tokens - int(max_tokens * history_ratio)``, so a resumed session's recap can
    never be squeezed out by a greedy retrieval pass that ran first. It is deliberately
    *not* an entry in ``ratio`` (which sums to ~1.0 across knowledge *classes* -- history
    is not a knowledge class, and folding it in there would make every pack's class
    ratios mean something different depending on whether the phase resumes).

    Default ``0.0`` = no reservation, which is the behaviour of every phase that never
    declared one.
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
    order is the evaluation order (first match wins), matching the interpreter loop."""

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
    """A phase's ``effects:`` entry, applied atomically with its transition:
    ``state.<set>`` is assigned the result of evaluating the ``to`` CEL expression."""

    model_config = ConfigDict(extra="forbid")

    set: str
    to: str


class AwaitSpec(BaseModel):
    """The interrupt primitive: a phase suspends until satisfied or
    ``timeout`` elapses, at which point ``on_timeout`` is the transition target.

    Two things are worth waiting for. ``human_input`` is the original: a person has to
    act. ``delegated_work`` is the other one this primitive always implied -- the phase
    handed work to coding agents, and the phases after it are about that work, so running
    them before it exists makes the flow incoherent rather than fast. A review phase that
    reviews nothing and a merge phase with an empty queue both reported success on a
    session whose branches had not been built yet.

    The interpreter treats both identically: it yields, and something satisfies. What
    differs is who -- a person through the HTTP endpoint, or the worker, when the last
    job belonging to the session finishes.

    ``reminder_at`` is pacing data, not new engine semantics: the elapsed duration
    after which a human who hasn't acted gets one nudge. the timeout sweep reads it;
    the interpreter never sees it. ``None`` inherits the definition-level
    ``pacing.reminder_at``, and if that is absent too, no reminder is sent -- silence is
    the correct default for a process whose author never asked for one."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["human_input", "delegated_work"]
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
    """Definition-level pacing defaults: "each actor has 48h, reminder at
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


class PhaseCompletionSpec(BaseModel):
    """What a phase must have produced before it is allowed to move on.

    A phase ends when its actor entries are used up, which says the turns were spent and
    nothing about whether the work happened. Observed on a live eight-beat run: the
    referee opened a fight, the fight was good, and it kept going while the flow slid
    underneath it -- four beats advanced on turn budget while the fiction never left the
    first encounter. The beat that was supposed to stage a different enemy was spent on
    more rounds of the previous one, and nothing anywhere reported a problem.

    The requirements are deliberately *counts over what this phase itself recorded*, not
    CEL over the world. A predicate that could read anything would make validation
    undecidable and make resume depend on re-evaluating state; a count of rows written
    since the phase began is cheap, total, and means the same thing on a replay.

    Every field is optional and defaults to "no requirement", so a phase that declares
    nothing behaves exactly as it always has.
    """

    model_config = ConfigDict(extra="forbid")

    # Deterministic results (dice, checks, scored outcomes) recorded in this phase.
    # `1` is the useful value for "this beat must actually resolve something".
    resolutions: int = Field(default=0, ge=0, le=1000)
    # Turns that actually landed a message in this phase.
    messages: int = Field(default=0, ge=0, le=1000)
    # Side-effecting tool calls recorded in this phase.
    tool_calls: int = Field(default=0, ge=0, le=1000)
    # Entity field writes or state-machine moves recorded in this phase -- "something in
    # the world changed", as distinct from "something was rolled".
    entity_changes: int = Field(default=0, ge=0, le=1000)
    # Entities belonging to this session that were created or written in this phase.
    # Distinct from entity_changes, which counts state-change rows: **creating** a record
    # writes none of those, so a beat whose whole job is to make something counted zero
    # against entity_changes and was reported unmet while three of the things sat in the
    # table. Measured from the row's own updated_at, which is set on insert and on every
    # write -- so this answers "did the cast change in this beat", not "were new ones
    # added"; there is no created_at on the row to ask the narrower question.
    entities_touched: int = Field(default=0, ge=0, le=1000)

    # What to do when the actors are exhausted and the requirement is not met.
    #   repeat -- run the phase's actors again, up to `max_repeats`, then move on
    #   hold -- the same, but pause the session for a human when repeats run out
    #   warn -- record that it was unmet and move on regardless
    # Moving on is the default end state in every case except `hold`: a campaign stuck
    # forever on a beat nobody can satisfy is a worse failure than a thin beat.
    on_unmet: Literal["repeat", "hold", "warn"] = "repeat"
    max_repeats: int = Field(default=1, ge=0, le=10)

    def is_declared(self) -> bool:
        return bool(
            self.resolutions
            or self.messages
            or self.tool_calls
            or self.entity_changes
            or self.entities_touched
        )


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
    # How many tool calls one turn of this phase may make before the runtime calls it a
    # runaway loop. The default guards against a model that never stops calling; it is
    # wrong for a phase whose work genuinely takes more. Basic Fantasy character creation
    # is six ability rolls and a sheet -- seven calls against a cap of eight, so a single
    # re-roll ended the turn, and the session, with "tool loop exceeded".
    max_tool_calls: int | None = Field(default=None, ge=1, le=64)
    budget: BudgetSpec | None = None
    gates: list[GateSpec] = Field(default_factory=list)
    effects: list[EffectSpec] = Field(default_factory=list)
    await_field: AwaitSpec | None = Field(default=None, alias="await")
    flags: list[str] = Field(default_factory=list)
    # What this phase must have produced before it may transition. Absent = no
    # requirement, which is what every phase did before this existed.
    requires: PhaseCompletionSpec | None = None
    tools: list[str] = Field(default_factory=list)
    # Which registered remote MCP tools the ACTING persona may use in this phase, by
    # tool name. None (the default) keeps the legacy behaviour -- every workspace
    # remote tool is offered; [] offers none; a list is an allowlist. This is what lets
    # a flow hand an oracle to its facilitator alone: put the tool on the phases only
    # the facilitator acts in, and [] on everyone else's.
    remote_tools: list[str] | None = None
    on_complete: str | None = None

    def history_slice_tokens(self) -> int:
        """the phase budget's declared history reservation, in tokens. ``0`` for a
        phase with no budget or no ``history_ratio``, so a phase that never declared one
        keeps its full retrieval budget."""
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
