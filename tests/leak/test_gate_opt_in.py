"""The gate's opt-in ladder (the rework): every rung that must NOT cost a model call.

1. workspace toggle off (the default) -> no gate call even in a held_by_actor phase
   with real held secrets; concealed-by-default still holds (plaintext absent).
2. toggle on but the actor holds NO secrets -> no gate call (no candidates, no charge).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from sqlalchemy import select, text

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
from core.secrets.models import DisclosureDecisionRow, SecretHolderRow, SecretRow
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_DIM = 1024


class _StubEmbedding:
    model_name = "stub-embed"

    async def embed(self, req: EmbedRequest) -> list[list[float]]:
        return [[1.0] + [0.0] * (_DIM - 1) for _ in req.texts]


class _CountingProvider:
    """Counts structured (gate) calls; any structured call in these tests is a bug."""

    def __init__(self) -> None:
        self.structured_calls = 0

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        yield Chunk(text="a perfectly ordinary reply.")

    async def generate_structured(self, req: GenerationRequest, schema: type[ModelT]) -> ModelT:
        self.structured_calls += 1
        raise AssertionError("the gate ran on a rung that must not fire it")

    def count_tokens(self, text_: str, model: str) -> int:
        return max(1, len(text_) // 4)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(supports_prompt_caching=False)


def _held_by_actor_phase() -> PhaseSpec:
    return PhaseSpec(
        label_key="phase.turn",
        actors=[],
        visibility=VisibilitySpec(
            knowledge_classes=[], scopes=[], entity_fields=[], secrets="held_by_actor"
        ),
        budget=BudgetSpec(ratio={}, max_tokens=512),
        tools=[],
    )


async def _table(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, "optin-profile", "echo", "echo-1", encryptor=IdentityEncryptor()
    )
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text("update persona set agent_id=:a where id=:p"),
            {"a": profile.id, "p": persona_id},
        )
    return tenant_id, workspace_id, persona_id, sess.id


async def _plant_secret(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, persona_id: uuid.UUID
) -> None:
    async with tenant_scope(tenant_id) as session:
        principal_id = (
            await session.execute(
                text("select principal_id from persona where id=:p"), {"p": persona_id}
            )
        ).scalar_one()
        secret = SecretRow(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            subject_kind="agent",
            subject_id=persona_id,
            content_ciphertext=IdentityEncryptor().encrypt("A-FACT-NOBODY-MAY-SEE"),
            gist="what happened at the gala",
            hint_text=None,
            behavioral_directive=None,
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


async def _run_turn(tenant_id, workspace_id, persona_id, session_id, provider) -> None:
    rule_row = await get_or_create_default_rule_system(tenant_id)
    result = await run_one_persona_turn(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        persona_id=persona_id,
        phase=_held_by_actor_phase(),
        phase_key="turn",
        event_seq=0,
        model_provider_factory=lambda _p: provider,
        embedding_provider=_StubEmbedding(),
        rule_system=RuleSystemDefinition.from_row(rule_row),
        rule_system_id=rule_row.id,
        encryptor=IdentityEncryptor(),
    )
    assert result.message_id is not None


async def test_toggle_off_means_no_gate_call_and_still_no_leak(db_available: None) -> None:
    tenant_id, workspace_id, persona_id, session_id = await _table("gateoff")
    await _plant_secret(tenant_id, workspace_id, persona_id)  # secrets exist...
    provider = _CountingProvider()
    await _run_turn(tenant_id, workspace_id, persona_id, session_id, provider)

    assert provider.structured_calls == 0
    async with tenant_scope(tenant_id) as session:
        decision = await session.scalar(
            select(DisclosureDecisionRow).where(DisclosureDecisionRow.session_id == session_id)
        )
        assert decision is None
        leaked = await session.scalar(
            text("select count(*) from message where session_id=:s and content_md like :p"),
            {"s": session_id, "p": "%A-FACT-NOBODY-MAY-SEE%"},
        )
        assert leaked == 0


async def test_toggle_on_but_no_held_secrets_means_no_gate_call(db_available: None) -> None:
    tenant_id, workspace_id, persona_id, session_id = await _table("gateon")
    async with tenant_scope(tenant_id) as session:
        row = await session.get(Workspace, workspace_id)
        row.settings = {**dict(row.settings), "secrets_gate": True}
    provider = _CountingProvider()
    await _run_turn(tenant_id, workspace_id, persona_id, session_id, provider)

    assert provider.structured_calls == 0
    async with tenant_scope(tenant_id) as session:
        decision = await session.scalar(
            select(DisclosureDecisionRow).where(DisclosureDecisionRow.session_id == session_id)
        )
        assert decision is None
