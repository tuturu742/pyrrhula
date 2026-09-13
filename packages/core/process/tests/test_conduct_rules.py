"""workspace.settings.conduct_rules reaches the turn's system prompt -- formatting is
per-workspace CONTENT (user-editable), not hardcoded core behavior. The minimal
identity line ships always (it derives from core's transcript serialization)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from sqlalchemy import text

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent
from core.agents.seed import seed_dev_agent
from core.ports.embedding import EmbedRequest
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest
from core.process.dsl.schema import BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.live_session import run_one_persona_turn
from core.process.skeleton import create_session
from core.resolution.rule_system import (
    RuleSystemDefinition,
    get_or_create_default_rule_system,
)
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_DIM = 1024
_RULES = "Reply in haiku only, and never mention the weather."


class _StubEmbedding:
    model_name = "stub-embed"

    async def embed(self, req: EmbedRequest) -> list[list[float]]:
        return [[1.0] + [0.0] * (_DIM - 1) for _ in req.texts]


@dataclass
class _Capture:
    payloads: list[str] = field(default_factory=list)

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        self.payloads.append(str(req.messages))
        yield Chunk(text="ok.")

    def count_tokens(self, text_: str, model: str) -> int:
        return 1

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(supports_prompt_caching=False)


async def test_conduct_rules_and_identity_line_reach_the_prompt(db_available: None) -> None:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"cr-{uuid.uuid4().hex[:8]}")
    async with tenant_scope(tenant_id) as session:
        ws = await session.get(Workspace, workspace_id)
        ws.settings = {**dict(ws.settings), "conduct_rules": _RULES}
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, "cr-profile", "echo", "echo-1", encryptor=IdentityEncryptor()
    )
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text("update persona set agent_id=:a, name='Testa McTest' where id=:p"),
            {"a": profile.id, "p": persona_id},
        )

    rule_row = await get_or_create_default_rule_system(tenant_id)
    provider = _Capture()
    result = await run_one_persona_turn(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=sess.id,
        persona_id=persona_id,
        phase=PhaseSpec(
            label_key="phase.turn",
            actors=[],
            visibility=VisibilitySpec(
                knowledge_classes=[], scopes=[], entity_fields=[], secrets="none"
            ),
            budget=BudgetSpec(ratio={}, max_tokens=256),
            tools=[],
        ),
        phase_key="turn",
        event_seq=0,
        model_provider_factory=lambda _p: provider,
        embedding_provider=_StubEmbedding(),
        rule_system=RuleSystemDefinition.from_row(rule_row),
        rule_system_id=rule_row.id,
        encryptor=IdentityEncryptor(),
    )
    assert result.message_id is not None
    payload = provider.payloads[0]
    assert _RULES in payload  # the workspace's own rules
    assert "you speak only as Testa McTest" in payload  # the core identity line
