"""S1's regression lock: a LIVE-TURN-shaped test proving `run_one_persona_turn`
actually invokes the disclosure machinery -- the audit found every component built and
none of it reachable from a real turn. This test seeds a held secret with a real gist
embedding, drives a full persona turn with scripted providers, and asserts:

1. the gate ran and persisted a `DisclosureDecision` for this (session, event_seq);
2. the concealed plaintext is ABSENT from the persisted context manifest, which
   carries a `secret` redaction instead (exclusion at selection, INV-8);
3. the turn's message persisted and does not contain the plaintext (leak check ran
   over a clean reply).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import cast

from sqlalchemy import select, text

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent
from core.agents.seed import seed_dev_agent
from core.assembler.models import ContextManifestRow
from core.ports.embedding import EmbedRequest
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest, ModelT
from core.process.dsl.schema import BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.live_session import run_one_persona_turn
from core.process.skeleton import create_session
from core.resolution.rule_system import RuleSystemDefinition, get_or_create_default_rule_system
from core.secrets.gate import GateResponseSchema
from core.secrets.models import DisclosureDecisionRow, SecretHolderRow
from core.sessions.models import MessageRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_PLAINTEXT = "THE-MURDERER-IS-LORD-CASSIAN-VELT-CONCEALED"
_DIM = 1024


async def _enable_secrets_gate(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    """The gate is workspace-opt-in (settings.secrets_gate); tests that exercise it
    accept the cost explicitly, exactly like a real workspace owner."""
    from core.tenancy.models import Workspace

    async with tenant_scope(tenant_id) as session:
        row = await session.get(Workspace, workspace_id)
        row.settings = {**dict(row.settings), "secrets_gate": True}


class _StubEmbedding:
    """Deterministic unit vectors; identical text -> identical vector, so the gate's
    prefilter fires (gist vs recent-turns similarity == 1 when texts align)."""

    model_name = "stub-embed"

    async def embed(self, req: EmbedRequest) -> list[list[float]]:
        out: list[list[float]] = []
        for text_ in req.texts:
            vec = [0.0] * _DIM
            vec[hash(text_) % 7] = 1.0  # a few distinct directions
            out.append(vec)
        return out


@dataclass
class _TurnProvider:
    """generate() -> a fixed clean reply; generate_structured() -> a scripted gate
    verdict (conceal). One provider serves both the gate and the generation call."""

    gate_response: GateResponseSchema

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        yield Chunk(text="I keep my own counsel about that night.")

    async def generate_structured(self, req: GenerationRequest, schema: type[ModelT]) -> ModelT:
        assert "THE-MURDERER" not in str(req.messages), "gate payload carried plaintext"
        return cast(ModelT, self.gate_response)

    def count_tokens(self, text_: str, model: str) -> int:
        return max(1, len(text_) // 4)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(supports_prompt_caching=False)


def _phase() -> PhaseSpec:
    return PhaseSpec(
        label_key="phase.turn",
        actors=[],
        visibility=VisibilitySpec(
            knowledge_classes=[], scopes=[], entity_fields=[], secrets="held_by_actor"
        ),
        budget=BudgetSpec(ratio={}, max_tokens=512),
        tools=[],
    )


async def test_live_turn_invokes_gate_and_excludes_concealed_plaintext(
    db_available: None,
) -> None:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"seam-{uuid.uuid4().hex[:8]}")
    await _enable_secrets_gate(tenant_id, workspace_id)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, "seam-profile", "echo", "echo-1", encryptor=IdentityEncryptor()
    )
    async with tenant_scope(tenant_id) as session:
        principal_id = (
            await session.execute(
                text("select principal_id from persona where id=:p"), {"p": persona_id}
            )
        ).scalar_one()
        await session.execute(
            text("update persona set agent_id=:a where id=:p"),
            {"a": profile.id, "p": persona_id},
        )

    from core.secrets.models import SecretRow

    async with tenant_scope(tenant_id) as session:
        secret = SecretRow(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            subject_kind="agent",
            subject_id=persona_id,
            content_ciphertext=IdentityEncryptor().encrypt(_PLAINTEXT),
            gist="what really happened on the night of the gala",
            hint_text="They visibly stiffen when the gala is mentioned.",
            behavioral_directive="Deflect questions about the gala without lying outright.",
            scope_key="workspace_public",
        )
        session.add(secret)
        await session.flush()
        session.expunge(secret)
    embedder = _StubEmbedding()
    # First turn: conversation is empty -> live_session embeds query_text or " ".
    gist_vec = (await embedder.embed(EmbedRequest(model="stub", texts=[" "])))[0]
    async with tenant_scope(tenant_id) as session:
        session.add(
            SecretHolderRow(
                tenant_id=tenant_id,
                secret_id=secret.id,
                holder_principal_id=principal_id,
                holder_kind="author",
            )
        )
        # The gist embedding ALIGNED with the first turn's query embedding, so the
        # prefilter fires deterministically.
        await session.execute(
            text("update secret set gist_embedding = CAST(:v AS vector) where id = :id"),
            {"v": "[" + ",".join(str(x) for x in gist_vec) + "]", "id": secret.id},
        )

    gate_response = GateResponseSchema.model_validate(
        {
            "decisions": [
                {
                    "secret_id": str(secret.id),
                    "action": "conceal",
                    "rationale": "a stranger is probing",
                    "confidence": 0.95,
                }
            ],
            "posture": "guarded",
        }
    )
    provider = _TurnProvider(gate_response=gate_response)
    rule_system_row = await get_or_create_default_rule_system(tenant_id)

    event_seq = 0
    result = await run_one_persona_turn(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=sess.id,
        persona_id=persona_id,
        phase=_phase(),
        phase_key="turn",
        event_seq=event_seq,
        model_provider_factory=lambda _provider: provider,
        embedding_provider=embedder,
        rule_system=RuleSystemDefinition.from_row(rule_system_row),
        rule_system_id=rule_system_row.id,
        encryptor=IdentityEncryptor(),
    )
    assert result.message_id is not None

    async with tenant_scope(tenant_id) as session:
        decision = await session.scalar(
            select(DisclosureDecisionRow).where(
                DisclosureDecisionRow.session_id == sess.id,
                DisclosureDecisionRow.event_seq == event_seq,
            )
        )
        assert decision is not None, "the live turn never ran the disclosure gate"

        manifest = await session.scalar(
            select(ContextManifestRow).where(
                ContextManifestRow.session_id == sess.id,
                ContextManifestRow.event_seq == event_seq,
            )
        )
        assert manifest is not None
        redaction_types = {r.get("type") for r in manifest.redactions}
        assert "secret" in redaction_types, "no secret redaction recorded in the manifest"

        message = await session.get(MessageRow, result.message_id)
        assert message is not None
        assert _PLAINTEXT not in message.content_md

        # INV-8: exclusion, not scrubbing -- the concealed plaintext must be absent
        # from everything the manifest persisted about this turn's context.
        assert _PLAINTEXT not in str(manifest.entries)


async def test_secrets_none_phase_never_gates(db_available: None) -> None:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(
        slug=f"seam-none-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, "seam-none", "echo", "echo-1", encryptor=IdentityEncryptor()
    )
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text("update persona set agent_id=:a where id=:p"),
            {"a": profile.id, "p": persona_id},
        )

    phase = _phase()
    phase = phase.model_copy(
        update={
            "visibility": VisibilitySpec(
                knowledge_classes=[], scopes=[], entity_fields=[], secrets="none"
            )
        }
    )
    provider = _TurnProvider(
        gate_response=GateResponseSchema.model_validate({"decisions": [], "posture": "n/a"})
    )
    rule_system_row = await get_or_create_default_rule_system(tenant_id)
    result = await run_one_persona_turn(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=sess.id,
        persona_id=persona_id,
        phase=phase,
        phase_key="turn",
        event_seq=0,
        model_provider_factory=lambda _provider: provider,
        embedding_provider=_StubEmbedding(),
        rule_system=RuleSystemDefinition.from_row(rule_system_row),
        rule_system_id=rule_system_row.id,
        encryptor=IdentityEncryptor(),
    )
    assert result.message_id is not None
    async with tenant_scope(tenant_id) as session:
        decision = await session.scalar(
            select(DisclosureDecisionRow).where(DisclosureDecisionRow.session_id == sess.id)
        )
        assert decision is None, "a secrets='none' phase must never invoke the gate"
