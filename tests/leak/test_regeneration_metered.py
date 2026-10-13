"""The leak check's regeneration is a real model call: it must use the connection's own
key, and it must be metered on the turn's message (rule 11).

Before: the regeneration built its request without `api_key`, so it worked only against
providers whose key sat in the process environment (every sealed connection, OpenRouter
among them, failed), and it wrote no usage row at all.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import cast

from sqlalchemy import select, text

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent
from core.agents.seed import seed_dev_agent
from core.audit.models import UsageRecordRow
from core.ports.embedding import EmbedRequest
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest, ModelT
from core.process.dsl.schema import BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.live_session import run_one_persona_turn
from core.process.skeleton import create_session
from core.resolution.rule_system import RuleSystemDefinition, get_or_create_default_rule_system
from core.secrets.gate import GateResponseSchema
from core.secrets.models import SecretHolderRow, SecretRow
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_SECRET = "the gardener poisoned the soup at nine with arsenic from the shed"
_KEY = "sk-regen-test-key"
_DIM = 1024


class _OneDirectionEmbedding:
    """Every text gets the same vector, so the gate's prefilter always fires; the leak
    check's word-overlap test decides what leaked."""

    model_name = "stub-embed"

    async def embed(self, req: EmbedRequest) -> list[list[float]]:
        return [[1.0] + [0.0] * (_DIM - 1) for _ in req.texts]


@dataclass
class _LeakThenCleanProvider:
    gate_response: GateResponseSchema
    replies: list[str] = field(
        default_factory=lambda: [
            f"Fine, I confess: {_SECRET}.",
            "I keep my own counsel about that night.",
        ]
    )
    keys: list[str | None] = field(default_factory=list)

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        self.keys.append(req.api_key)
        yield Chunk(text=self.replies.pop(0))

    async def generate_structured(self, req: GenerationRequest, schema: type[ModelT]) -> ModelT:
        return cast(ModelT, self.gate_response)

    def count_tokens(self, text_: str, model: str) -> int:
        return max(1, len(text_) // 4)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(supports_prompt_caching=False)


async def test_regeneration_uses_the_connection_key_and_is_metered(db_available: None) -> None:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"regen-{uuid.uuid4().hex[:8]}")
    async with tenant_scope(tenant_id) as session:
        ws = await session.get(Workspace, workspace_id)
        assert ws is not None
        ws.settings = {**dict(ws.settings), "secrets_gate": True}
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, "regen-profile", "echo", "echo-1", api_key=_KEY, encryptor=IdentityEncryptor()
    )
    async with tenant_scope(tenant_id) as session:
        principal_id = (
            await session.execute(
                text("select principal_id from persona where id=:p"), {"p": persona_id}
            )
        ).scalar_one()
        await session.execute(
            text("update persona set agent_id=:a where id=:p"), {"a": profile.id, "p": persona_id}
        )
        secret = SecretRow(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            subject_kind="agent",
            subject_id=persona_id,
            content_ciphertext=IdentityEncryptor().encrypt(_SECRET),
            gist="what happened in the kitchen",
            behavioral_directive="Never say what happened in the kitchen.",
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
            {"v": "[" + ",".join(["1"] + ["0"] * (_DIM - 1)) + "]", "id": secret.id},
        )
    gate = GateResponseSchema.model_validate(
        {
            "decisions": [
                {
                    "secret_id": str(secret.id),
                    "action": "conceal",
                    "rationale": "x",
                    "confidence": 0.9,
                }
            ],
            "posture": "guarded",
        }
    )
    provider = _LeakThenCleanProvider(gate_response=gate)
    rule_system_row = await get_or_create_default_rule_system(tenant_id)
    result = await run_one_persona_turn(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=sess.id,
        persona_id=persona_id,
        phase=PhaseSpec(
            label_key="phase.turn",
            actors=[],
            visibility=VisibilitySpec(
                knowledge_classes=[], scopes=[], entity_fields=[], secrets="held_by_actor"
            ),
            budget=BudgetSpec(ratio={}, max_tokens=512),
            tools=[],
        ),
        phase_key="turn",
        event_seq=0,
        model_provider_factory=lambda _p: provider,
        embedding_provider=_OneDirectionEmbedding(),
        rule_system=RuleSystemDefinition.from_row(rule_system_row),
        rule_system_id=rule_system_row.id,
        encryptor=IdentityEncryptor(),
    )
    assert result.message_id is not None
    assert provider.keys == [_KEY, _KEY], "the regeneration must carry the connection's key"
    async with tenant_scope(tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(UsageRecordRow).where(
                        UsageRecordRow.session_id == sess.id,
                        UsageRecordRow.purpose == "generation",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 2, "the turn's call and its regeneration, both metered"
    assert {r.message_id for r in rows} == {result.message_id}
