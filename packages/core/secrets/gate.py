"""The disclosure gate: the structured pre-decision
that chooses conceal/hint/reveal_full per secret before generation. Sees gists only,
never `secret.content` -- a security boundary, not an optimisation, since it's what lets
this run on a small/cheap/possibly-cloud model under D14 without the secret leaving the
building.

Deliberately free of `core.secrets.repo`: this module cannot import it (INV-1 restricts
that module to `core.assembler`/`core.overseer`) and doesn't need to -- every input here
is a plain argument the caller (eventually `core.assembler.context_assembler`, E2.6)
already resolved. This module persists its own output through `core.secrets.decisions`,
which is *not* INV-1-restricted (writing a decision never reads `content_ciphertext`).

The gate decides; E2.6 enforces. Neither is useful without the other.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from core.agents.models import Agent
from core.ports.model_provider import GenerationRequest, ModelProvider
from core.secrets.decisions import record_gate_decision

_VALID_ACTIONS = frozenset({"conceal", "hint", "reveal_full"})
_PURPOSE = "gate"


class GateValidationError(Exception):
    """A gate response failed strict schema/business-rule validation -- a hallucinated
    secret_id, an unknown action, an out-of-range confidence, or a missing/extra decision
    relative to the fired set. Always results in fail-closed-to-conceal; never escapes to
    the turn loop as an unhandled exception (this task's own acceptance criterion)."""


@dataclass(frozen=True)
class CandidateSecret:
    """What the gate is allowed to see for one secret -- `gist`/`gist_embedding` only,
    never `content`. The caller resolves this (eventually via `core.secrets.repo`, which
    this module itself cannot import) before calling `run_disclosure_gate`."""

    secret_id: uuid.UUID
    gist: str
    gist_embedding: Sequence[float]


@dataclass(frozen=True)
class SecretDecision:
    secret_id: uuid.UUID
    action: str
    rationale: str
    confidence: float


@dataclass(frozen=True)
class GateResult:
    decisions: tuple[SecretDecision, ...]
    posture: str | None
    fired: bool
    model_call_made: bool
    decision_row_id: uuid.UUID | None
    failure: str | None = None


class _DecisionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret_id: str
    action: str
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)


class GateResponseSchema(BaseModel):
    """The strict JSON schema the gate call must produce -- on a local model this
    is what grammar-constrained decoding (Ollama JSON-schema format / GBNF) targets;
    Q5's all-modes decision makes that mandatory, not a nice-to-have, though the actual
    grammar wiring is an adapter-level concern (`core.ports.model_provider
    .generate_structured`), not this module's."""

    model_config = ConfigDict(extra="forbid")

    decisions: list[_DecisionSchema]
    posture: str


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def compute_fired_secrets(
    candidates: Sequence[CandidateSecret],
    recent_turns_embedding: Sequence[float],
    phase_flags: frozenset[str],
    tau: float,
) -> tuple[CandidateSecret, ...]:
    """'s prefilter, no model call: fires per-candidate iff `"mechanical" not in
    phase_flags` and `cos_sim(recent_turns_embedding, candidate.gist_embedding) > tau`.
    `candidates` is assumed already filtered to "agent's held secrets ∩ active scope" by
    the caller -- that intersection needs `core.secrets.repo`/holder reads this module
    can't perform itself. tau is set low and configurable by the caller: a false fire
    costs a cent; a false skip costs the plot."""
    if "mechanical" in phase_flags:
        return ()
    return tuple(
        c for c in candidates if _cosine_similarity(recent_turns_embedding, c.gist_embedding) > tau
    )


def _conceal_all(
    candidates: Sequence[CandidateSecret], rationale: str
) -> tuple[SecretDecision, ...]:
    return tuple(
        SecretDecision(secret_id=c.secret_id, action="conceal", rationale=rationale, confidence=1.0)
        for c in candidates
    )


def _build_gate_request(
    fired: Sequence[CandidateSecret],
    *,
    persona_summary: str,
    axis_values: dict[str, int],
    phase_label: str,
    recent_turns: Sequence[str],
    addressed_by: str | None,
    model: str,
    api_base: str | None,
    egress_policy: dict[str, list[str]] | None = None,
    axis_definitions: Sequence[object] = (),
    api_key: str | None = None,
    params: dict[str, object] | None = None,
) -> GenerationRequest:
    """The request payload contains `{secret_id, gist}` pairs and nothing else about each
    secret -- structurally, since `CandidateSecret` has no `content` field for this
    function to even reach for. `purpose='gate'` : may be cloud-permitted even in
    hybrid mode because it sees gists only, never plaintext."""
    system = (
        "You are the disclosure gate for a narrative agent. For each listed secret "
        "(identified only by a topical gist, never the underlying fact), decide whether "
        "the agent should conceal it, offer a vague hint, or reveal it fully this turn, "
        "given the agent's disposition, the phase, recent conversation, and who is being "
        "addressed. Respond with the exact JSON schema requested -- one decision per "
        'listed secret_id. The "action" field MUST be exactly one of these three '
        'strings: "conceal", "hint", "reveal_full". No other value is accepted.'
    )
    # Disposition, with MEANING: a bare `{"malice": 85}` gives the gate model a number
    # it cannot interpret. For each gate-bound axis that has a set value, send the
    # value, its declared range, and the axis's own semantics prose. Axes without a
    # definition (or the whole set, when no definitions were passed) fall back to the
    # raw numbers rather than being dropped -- less information beats none.
    defs_by_key = {getattr(d, "key", None): d for d in axis_definitions}
    disposition: list[dict[str, object]] = []
    for key, value in axis_values.items():
        axis = defs_by_key.get(key)
        if axis is None:
            disposition.append({"axis": key, "value": value})
            continue
        if not any(b.get("kind") == "gate" for b in getattr(axis, "bindings", [])):
            continue  # low-stakes axes (chattiness, ...) are not the gate's business
        disposition.append(
            {
                "axis": key,
                "value": value,
                "range": [getattr(axis, "range_min", 0), getattr(axis, "range_max", 100)],
                "meaning": getattr(axis, "semantics_md", ""),
            }
        )
    payload = {
        "persona_summary": persona_summary,
        "secrets": [{"secret_id": str(c.secret_id), "gist": c.gist} for c in fired],
        "disposition": disposition,
        "phase": phase_label,
        "recent_turns": list(recent_turns),
        "addressed_by": addressed_by,
    }
    return GenerationRequest(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        purpose=_PURPOSE,
        max_tokens=400,
        api_base=api_base,
        api_key=api_key,
        egress_policy=egress_policy or {},
        params=dict(params or {}),
    )


# Unambiguous near-misses a schema-following model still produces (observed live:
# GPT-5.6 Luna answered "reveal", strict validation rejected it, and fail-closed
# CONCEALED a secret both the disposition and the gate's own judgement wanted
# revealed -- over-concealment born from an enum spelling). Only aliases with exactly
# one possible meaning are mapped; anything else still fails closed.
_ACTION_ALIASES = {
    "reveal": "reveal_full",
    "reveal_fully": "reveal_full",
    "full_reveal": "reveal_full",
    "disclose": "reveal_full",
    "withhold": "conceal",
    "hide": "conceal",
    "suppress": "conceal",
}


def _normalize_actions(response: GateResponseSchema) -> GateResponseSchema:
    changed = False
    decisions = []
    for decision in response.decisions:
        action = _ACTION_ALIASES.get(decision.action.strip().lower(), decision.action)
        if action != decision.action:
            decision = decision.model_copy(update={"action": action})
            changed = True
        decisions.append(decision)
    if not changed:
        return response
    return response.model_copy(update={"decisions": decisions})


def _validate_response(response: GateResponseSchema, fired: Sequence[CandidateSecret]) -> None:
    fired_ids = {str(c.secret_id) for c in fired}
    response_ids = {d.secret_id for d in response.decisions}
    if response_ids != fired_ids:
        raise GateValidationError(
            f"gate response covers {sorted(response_ids)}, expected exactly {sorted(fired_ids)}"
        )
    for d in response.decisions:
        if d.action not in _VALID_ACTIONS:
            raise GateValidationError(
                f"secret {d.secret_id}: unknown action {d.action!r}, "
                f"must be one of {sorted(_VALID_ACTIONS)}"
            )


async def run_disclosure_gate(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    event_seq: int,
    persona_id: uuid.UUID,
    candidates: Sequence[CandidateSecret],
    *,
    recent_turns_embedding: Sequence[float],
    recent_turns: Sequence[str],
    phase_flags: frozenset[str],
    phase_label: str,
    persona_summary: str,
    axis_values: dict[str, int],
    addressed_by: str | None,
    behavior_profile_version: int,
    agent: Agent,
    provider: ModelProvider,
    workspace_id: uuid.UUID | None = None,
    tau: float = 0.15,
    egress_policy: dict[str, list[str]] | None = None,
    axis_definitions: Sequence[object] = (),
    api_key: str | None = None,
) -> GateResult:
    """One batched call per agent-turn covering every fired secret -- never one call per
    secret. Not-fired means every candidate defaults to conceal with no model call (this
    task's own acceptance criterion); a fired batch that fails validation, times out, or
    hits a provider error *also* defaults every fired secret to conceal -- fail-closed,
    always, no exception ever escapes to the turn loop."""
    fired = compute_fired_secrets(candidates, recent_turns_embedding, phase_flags, tau)
    if not fired:
        return GateResult(
            decisions=_conceal_all(candidates, "prefilter did not fire"),
            posture=None,
            fired=False,
            model_call_made=False,
            decision_row_id=None,
        )

    model_string = f"{agent.provider}/{agent.model}"
    request = _build_gate_request(
        fired,
        persona_summary=persona_summary,
        axis_values=axis_values,
        phase_label=phase_label,
        recent_turns=recent_turns,
        addressed_by=addressed_by,
        model=model_string,
        api_base=agent.api_base,
        params=dict(agent.params or {}),
        egress_policy=egress_policy,
        axis_definitions=axis_definitions,
        api_key=api_key,
    )

    start = time.monotonic()
    try:
        response = await provider.generate_structured(request, GateResponseSchema)
        response = _normalize_actions(response)
        _validate_response(response, fired)
    except Exception as exc:  # noqa: BLE001 -- any failure at all fails closed, by design
        latency_ms = int((time.monotonic() - start) * 1000)
        decisions = _conceal_all(fired, f"gate call failed, concealing: {exc}")
        prompt_tokens = sum(
            provider.count_tokens(str(m.get("content") or ""), model_string)
            for m in request.messages
        )
        decision_row = await record_gate_decision(
            tenant_id,
            session_id,
            event_seq,
            persona_id,
            behavior_profile_version,
            [
                {
                    "secret_id": str(d.secret_id),
                    "action": d.action,
                    "rationale": d.rationale,
                    "confidence": d.confidence,
                }
                for d in decisions
            ],
            agent_id=agent.id,
            provider=agent.provider,
            model=agent.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=0,
            latency_ms=latency_ms,
            workspace_id=workspace_id,
        )
        return GateResult(
            decisions=decisions,
            posture=None,
            fired=True,
            model_call_made=True,
            decision_row_id=decision_row.id,
            failure=str(exc),
        )

    latency_ms = int((time.monotonic() - start) * 1000)
    decisions = tuple(
        SecretDecision(
            secret_id=uuid.UUID(d.secret_id),
            action=d.action,
            rationale=d.rationale,
            confidence=d.confidence,
        )
        for d in response.decisions
    )
    prompt_tokens = sum(
        provider.count_tokens(str(m.get("content") or ""), model_string) for m in request.messages
    )
    completion_tokens = provider.count_tokens(response.model_dump_json(), model_string)
    decision_row = await record_gate_decision(
        tenant_id,
        session_id,
        event_seq,
        persona_id,
        behavior_profile_version,
        [
            {
                "secret_id": str(d.secret_id),
                "action": d.action,
                "rationale": d.rationale,
                "confidence": d.confidence,
            }
            for d in decisions
        ],
        agent_id=agent.id,
        provider=agent.provider,
        model=agent.model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        workspace_id=workspace_id,
    )
    return GateResult(
        decisions=decisions,
        posture=response.posture,
        fired=True,
        model_call_made=True,
        decision_row_id=decision_row.id,
    )
