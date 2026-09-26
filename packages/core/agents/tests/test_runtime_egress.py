"""run_agent_turn attaches the TENANT's egress policy to every generation
request -- the enforcement lives inside the real provider adapter (check_egress), so
what the runtime must guarantee is that the policy actually arrives on the request."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent
from core.agents.runtime import run_agent_turn
from core.agents.seed import seed_dev_agent
from core.agents.tools import ToolRegistry
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest
from core.process.skeleton import create_session
from core.tenancy.egress import invalidate_egress_policy
from core.tenancy.models import Tenant
from core.tenancy.scope import tenant_scope, unscoped_session
from core.tenancy.seed import seed_dev_tenant


class _CapturingProvider:
    def __init__(self) -> None:
        self.requests: list[GenerationRequest] = []

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        self.requests.append(req)
        yield Chunk(text="fine.")

    def count_tokens(self, text: str, model: str) -> int:
        return 1

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(supports_prompt_caching=False)


async def test_generation_request_carries_tenant_egress_policy(db_available: None) -> None:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"egress-{uuid.uuid4().hex[:8]}")
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, "egress-profile", "echo", "echo-1", encryptor=IdentityEncryptor()
    )
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            __import__("sqlalchemy").text("update persona set agent_id=:a where id=:p"),
            {"a": profile.id, "p": persona_id},
        )
    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.settings = {**tenant.settings, "egress_policy": {"generation": ["local"]}}
    invalidate_egress_policy(tenant_id)

    provider = _CapturingProvider()
    result = await run_agent_turn(
        tenant_id,
        persona_id,
        sess.id,
        [{"role": "user", "content": "hello"}],
        model_provider_factory=lambda _p: provider,
        tool_registry=ToolRegistry(),
        idempotency_key=f"turn:{sess.id}:0",
        event_seq=0,
    )
    assert result.message_id is not None
    assert provider.requests, "no generation request captured"
    assert provider.requests[0].egress_policy == {"generation": ["local"]}
