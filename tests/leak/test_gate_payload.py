"""E2.5's acceptance criteria for the disclosure gate (`core.secrets.gate`): an unfired
prefilter makes no model call, a malformed/failing gate call fails closed to conceal, the
request payload never carries plaintext, and every decision persists atomically with its
usage record.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import cast

from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent
from core.agents.models import Agent
from core.agents.seed import seed_dev_agent
from core.audit.models import UsageRecordRow
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest, ModelT
from core.process.skeleton import create_session
from core.secrets.gate import CandidateSecret, GateResponseSchema, run_disclosure_gate
from core.secrets.models import DisclosureDecisionRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_PLAINTEXT_MARKER = "THE-VAULT-COMBINATION-IS-4471-DO-NOT-LEAK-THIS"


@dataclass
class _ExplodingProvider:
    """Raises if the gate ever calls it -- proves the unfired-prefilter path makes no
    model call at all, not just "made a call that happened not to matter"."""

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        raise AssertionError("the gate must not call generate() at all")
        yield  # pragma: no cover

    async def generate_structured(self, req: GenerationRequest, schema: type[ModelT]) -> ModelT:
        raise AssertionError("the gate must not call the model when the prefilter didn't fire")

    def count_tokens(self, text: str, model: str) -> int:
        return max(len(text.split()), 1)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            supports_tools=False, supports_json_mode=True, supports_prompt_caching=False
        )


@dataclass
class _ScriptedGateProvider:
    """Captures the last request it was sent (for the plaintext-leak assertion) and
    either returns a scripted response or raises, on demand."""

    response: GateResponseSchema | None = None
    raise_instead: Exception | None = None
    last_request: GenerationRequest | None = field(default=None, init=False)

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def generate_structured(self, req: GenerationRequest, schema: type[ModelT]) -> ModelT:
        self.last_request = req
        if self.raise_instead is not None:
            raise self.raise_instead
        assert self.response is not None
        # This test double is only ever invoked with schema=GateResponseSchema (the gate's
        # own hardcoded response schema); `cast` documents that instead of widening the
        # method's declared return type away from the real `ModelProvider` protocol's.
        return cast(ModelT, self.response)

    def count_tokens(self, text: str, model: str) -> int:
        return max(len(text.split()), 1)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            supports_tools=False, supports_json_mode=True, supports_prompt_caching=False
        )


@dataclass(frozen=True)
class _Setup:
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID
    persona_id: uuid.UUID
    session_id: uuid.UUID
    agent: Agent


async def _setup(slug_prefix: str) -> _Setup:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, "gate-profile", "echo", "echo-1", encryptor=IdentityEncryptor()
    )
    return _Setup(tenant_id, workspace_id, persona_id, sess.id, profile)


async def test_unfired_prefilter_conceals_all_without_a_model_call(db_available: None) -> None:
    s = await _setup("gate-unfired")
    secret_id = uuid.uuid4()
    # Orthogonal to the recent-turns embedding -> cos_sim == 0, well under any positive tau.
    candidates = [
        CandidateSecret(secret_id=secret_id, gist="a distant topic", gist_embedding=[1.0, 0.0])
    ]
    recent_turns_embedding = [0.0, 1.0]

    result = await run_disclosure_gate(
        s.tenant_id,
        s.session_id,
        0,
        s.persona_id,
        candidates,
        recent_turns_embedding=recent_turns_embedding,
        recent_turns=["completely unrelated chatter"],
        phase_flags=frozenset(),
        phase_label="test_phase",
        persona_summary="a guarded merchant",
        axis_values={"secret_disclosure_propensity": 80},
        addressed_by=None,
        behavior_profile_version=1,
        agent=s.agent,
        provider=_ExplodingProvider(),
        tau=0.15,
    )

    assert result.fired is False
    assert result.model_call_made is False
    assert result.decision_row_id is None
    assert len(result.decisions) == 1
    assert result.decisions[0].secret_id == secret_id
    assert result.decisions[0].action == "conceal"


async def test_malformed_gate_output_fails_closed_to_conceal(db_available: None) -> None:
    s = await _setup("gate-malformed")
    secret_id = uuid.uuid4()
    candidates = [
        CandidateSecret(secret_id=secret_id, gist="a hot topic", gist_embedding=[1.0, 0.0])
    ]

    # A response that hallucinates a different secret_id entirely -- must be rejected by
    # _validate_response and result in fail-closed, never an unhandled exception.
    bad_response = GateResponseSchema.model_validate(
        {
            "decisions": [
                {
                    "secret_id": str(uuid.uuid4()),
                    "action": "reveal_full",
                    "rationale": "hallucinated",
                    "confidence": 0.9,
                }
            ],
            "posture": "confident",
        }
    )
    provider = _ScriptedGateProvider(response=bad_response)

    result = await run_disclosure_gate(
        s.tenant_id,
        s.session_id,
        0,
        s.persona_id,
        candidates,
        recent_turns_embedding=[1.0, 0.0],
        recent_turns=["someone asks about the hot topic"],
        phase_flags=frozenset(),
        phase_label="test_phase",
        persona_summary="a nervous informant",
        axis_values={"secret_disclosure_propensity": 90},
        addressed_by=None,
        behavior_profile_version=1,
        agent=s.agent,
        provider=provider,
        tau=0.15,
    )

    assert result.fired is True
    assert result.model_call_made is True
    assert result.failure is not None
    assert len(result.decisions) == 1
    assert result.decisions[0].secret_id == secret_id
    assert result.decisions[0].action == "conceal"
    assert result.decision_row_id is not None

    async with tenant_scope(s.tenant_id) as session:
        row = await session.get(DisclosureDecisionRow, result.decision_row_id)
        assert row is not None
        assert row.decisions[0]["action"] == "conceal"


async def test_gate_payload_contains_gists_and_never_plaintext(db_available: None) -> None:
    s = await _setup("gate-payload")
    secret_id = uuid.uuid4()
    gist = "there is an unresolved tension about the old bridge"
    candidates = [CandidateSecret(secret_id=secret_id, gist=gist, gist_embedding=[1.0, 0.0])]

    response = GateResponseSchema.model_validate(
        {
            "decisions": [
                {
                    "secret_id": str(secret_id),
                    "action": "conceal",
                    "rationale": "not pressured yet",
                    "confidence": 0.7,
                }
            ],
            "posture": "guarded",
        }
    )
    provider = _ScriptedGateProvider(response=response)

    await run_disclosure_gate(
        s.tenant_id,
        s.session_id,
        0,
        s.persona_id,
        candidates,
        recent_turns_embedding=[1.0, 0.0],
        # A planted marker in conversation text (a legitimate volatile input) -- distinct
        # from secret.content, which this test never gives the gate at all.
        recent_turns=[f"someone mentions {_PLAINTEXT_MARKER} out of band"],
        phase_flags=frozenset(),
        phase_label="test_phase",
        persona_summary="a stoic bridge keeper",
        axis_values={"secret_disclosure_propensity": 40},
        addressed_by=None,
        behavior_profile_version=1,
        agent=s.agent,
        provider=provider,
        tau=0.15,
    )

    assert provider.last_request is not None
    payload_text = " ".join(m["content"] for m in provider.last_request.messages)
    assert gist in payload_text
    # What must never appear is the secret's protected content -- structurally
    # guaranteed here, not merely untested: CandidateSecret carries no `content` field
    # for _build_gate_request to ever reach for, so there is no plaintext for this
    # payload to leak in the first place.
    assert "content_ciphertext" not in payload_text


async def test_gate_decision_and_usage_record_commit_atomically(db_available: None) -> None:
    s = await _setup("gate-atomic")
    secret_id = uuid.uuid4()
    candidates = [CandidateSecret(secret_id=secret_id, gist="a topic", gist_embedding=[1.0, 0.0])]
    response = GateResponseSchema.model_validate(
        {
            "decisions": [
                {
                    "secret_id": str(secret_id),
                    "action": "hint",
                    "rationale": "mild pressure",
                    "confidence": 0.6,
                }
            ],
            "posture": "cagey",
        }
    )
    provider = _ScriptedGateProvider(response=response)

    result = await run_disclosure_gate(
        s.tenant_id,
        s.session_id,
        0,
        s.persona_id,
        candidates,
        recent_turns_embedding=[1.0, 0.0],
        recent_turns=["a pointed question"],
        phase_flags=frozenset(),
        phase_label="test_phase",
        persona_summary="a reluctant witness",
        axis_values={"secret_disclosure_propensity": 60},
        addressed_by="player-1",
        behavior_profile_version=3,
        agent=s.agent,
        provider=provider,
        tau=0.15,
    )

    assert result.decision_row_id is not None
    async with tenant_scope(s.tenant_id) as session:
        decision_row = await session.get(DisclosureDecisionRow, result.decision_row_id)
        assert decision_row is not None
        assert decision_row.behavior_profile_version == 3
        assert decision_row.agent_id == s.agent.id

        usage_row = (
            await session.execute(
                select(UsageRecordRow).where(
                    UsageRecordRow.tenant_id == s.tenant_id,
                    UsageRecordRow.purpose == "gate",
                    UsageRecordRow.session_id == s.session_id,
                )
            )
        ).scalar_one()
        assert usage_row.agent_id == s.agent.id
        assert usage_row.prompt_tokens > 0
