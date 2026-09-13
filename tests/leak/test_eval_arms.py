"""The arms' DIFFERENCE, locked at the seam: arm 2 (gate_no_exclusion) provably places
concealed plaintext into the generation request; arm 3 (default) provably does not.
Same table, same secret, same scripted gate verdict -- the only variable is the
`eval_arm` switch, so this is the benchmark's premise as a regression test."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import cast

from sqlalchemy import text

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent
from core.agents.seed import seed_dev_agent
from core.ports.embedding import EmbedRequest
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest, ModelT
from core.process.dsl.schema import BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.live_session import run_one_persona_turn
from core.process.skeleton import create_session
from core.resolution.rule_system import (
    RuleSystemDefinition,
    get_or_create_default_rule_system,
)
from core.secrets.gate import GateResponseSchema
from core.secrets.models import SecretHolderRow, SecretRow
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_PLAINTEXT = "ARM-TEST-THE-BUTLER-POISONED-THE-VINTAGE"
_DIM = 1024


class _StubEmbedding:
    model_name = "stub-embed"

    async def embed(self, req: EmbedRequest) -> list[list[float]]:
        return [[1.0] + [0.0] * (_DIM - 1) for _ in req.texts]


@dataclass
class _Provider:
    gate_response: GateResponseSchema | None
    seen_generation_payloads: list[str] = field(default_factory=list)
    gate_calls: int = 0

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        self.seen_generation_payloads.append(str(req.messages))
        yield Chunk(text="I have nothing to add about that night.")

    async def generate_structured(self, req: GenerationRequest, schema: type[ModelT]) -> ModelT:
        self.gate_calls += 1
        assert self.gate_response is not None, "gate ran in an arm that must not call it"
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


async def _table_with_secret() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"arms-{uuid.uuid4().hex[:8]}")
    async with tenant_scope(tenant_id) as session:
        ws = await session.get(Workspace, workspace_id)
        ws.settings = {**dict(ws.settings), "secrets_gate": True}
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, "arms-profile", "echo", "echo-1", encryptor=IdentityEncryptor()
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
        secret = SecretRow(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            subject_kind="agent",
            subject_id=persona_id,
            content_ciphertext=IdentityEncryptor().encrypt(_PLAINTEXT),
            gist="what happened to the vintage at the gala",
            hint_text="They glance at the cellar door.",
            behavioral_directive="Change the subject away from the wine.",
            scope_key="workspace_public",
        )
        session.add(secret)
        await session.flush()
        session.add(
            SecretHolderRow(
                tenant_id=tenant_id,
                secret_id=secret.id,
                holder_principal_id=principal_id,
                holder_kind="author",
            )
        )
        await session.execute(
            text("update secret set gist_embedding = CAST(:v AS vector) where id = :id"),
            {"v": "[" + ",".join(["1.0"] + ["0.0"] * (_DIM - 1)) + "]", "id": secret.id},
        )
    return tenant_id, workspace_id, persona_id, sess.id, secret.id


def _conceal_response(secret_id: uuid.UUID) -> GateResponseSchema:
    return GateResponseSchema.model_validate(
        {
            "decisions": [
                {
                    "secret_id": str(secret_id),
                    "action": "conceal",
                    "rationale": "a stranger probes",
                    "confidence": 0.9,
                }
            ],
            "posture": "guarded",
        }
    )


async def _run(tenant_id, workspace_id, persona_id, session_id, provider, eval_arm) -> None:
    rule_row = await get_or_create_default_rule_system(tenant_id)
    result = await run_one_persona_turn(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        persona_id=persona_id,
        phase=_phase(),
        phase_key="turn",
        event_seq=0,
        model_provider_factory=lambda _p: provider,
        embedding_provider=_StubEmbedding(),
        rule_system=RuleSystemDefinition.from_row(rule_row),
        rule_system_id=rule_row.id,
        encryptor=IdentityEncryptor(),
        eval_arm=eval_arm,
    )
    assert result.message_id is not None


async def test_arm3_default_excludes_plaintext_from_generation(db_available: None) -> None:
    tenant_id, workspace_id, persona_id, session_id, secret_id = await _table_with_secret()
    provider = _Provider(gate_response=_conceal_response(secret_id))
    await _run(tenant_id, workspace_id, persona_id, session_id, provider, eval_arm=None)
    assert provider.gate_calls == 1
    assert all(_PLAINTEXT not in p for p in provider.seen_generation_payloads), (
        "full pipeline let concealed plaintext into a generation request"
    )


async def test_arm2_gate_runs_but_plaintext_enters_generation(db_available: None) -> None:
    tenant_id, workspace_id, persona_id, session_id, secret_id = await _table_with_secret()
    provider = _Provider(gate_response=_conceal_response(secret_id))
    await _run(
        tenant_id, workspace_id, persona_id, session_id, provider, eval_arm="gate_no_exclusion"
    )
    assert provider.gate_calls == 1, "arm 2 must still run the gate"
    assert any(_PLAINTEXT in p for p in provider.seen_generation_payloads), (
        "arm 2 must place the plaintext in context -- that is what it measures"
    )


async def test_arm1_no_gate_and_plaintext_enters_generation(db_available: None) -> None:
    tenant_id, workspace_id, persona_id, session_id, _secret_id = await _table_with_secret()
    provider = _Provider(gate_response=None)  # any gate call raises
    await _run(tenant_id, workspace_id, persona_id, session_id, provider, eval_arm="prompt_only")
    assert provider.gate_calls == 0
    assert any(_PLAINTEXT in p for p in provider.seen_generation_payloads)
