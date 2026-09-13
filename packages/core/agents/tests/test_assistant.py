"""The workspace assistant: required, idempotent to ensure, and metered when it drafts.
Live-Postgres tests with a scripted ``ModelProvider`` double, matching
``test_editing``'s pattern (the assistant generalises those proposal flows)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from adapters.embedding.stub.provider import StubEmbeddingProvider
from core.agents.assistant import (
    ASSISTANT_KEY,
    UnknownAssistTaskError,
    assist,
    ensure_workspace_assistant,
)
from core.agents.authoring import get_agent
from core.agents.models import Agent
from core.audit.models import UsageRecordRow
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


@dataclass
class _ScriptedAssistProvider:
    text: str
    seen_purposes: list[str]

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        raise NotImplementedError
        yield  # pragma: no cover -- makes this an async generator for the Protocol

    async def generate_structured(self, req: GenerationRequest, schema: type) -> object:  # type: ignore[type-arg]
        self.seen_purposes.append(req.purpose)
        return schema(text=self.text)

    def count_tokens(self, text: str, model: str) -> int:
        return max(len(text.split()), 1)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            supports_tools=False, supports_json_mode=True, supports_prompt_caching=False
        )


async def test_ensure_workspace_assistant_is_idempotent(db_available: None) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"assist-ensure-{uuid.uuid4().hex[:8]}"
    )

    first = await ensure_workspace_assistant(tenant_id, workspace_id)
    second = await ensure_workspace_assistant(tenant_id, workspace_id)

    assert first.id == second.id
    assert first.key == ASSISTANT_KEY
    assert first.persona_type == "informational"
    assert (await get_agent(tenant_id, first.agent_id)) is not None

    async with tenant_scope(tenant_id) as session:
        profiles = (
            await session.execute(
                select(Agent).where(Agent.tenant_id == tenant_id, Agent.name == "Assistant model")
            )
        ).scalars()
        assert len(list(profiles)) == 1


async def test_assist_drafts_and_meters(db_available: None) -> None:
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=f"assist-draft-{uuid.uuid4().hex[:8]}"
    )
    persona = await ensure_workspace_assistant(tenant_id, workspace_id)
    profile = await get_agent(tenant_id, persona.agent_id)
    assert profile is not None

    async with tenant_scope(tenant_id) as session:
        viewer = await session.get(Principal, owner_id)
        assert viewer is not None
        session.expunge(viewer)

    provider = _ScriptedAssistProvider(text="You are a stern chronicler.", seen_purposes=[])
    result = await assist(
        tenant_id,
        workspace_id,
        viewer,
        task="draft_persona",
        subject="Chronicler",
        instruction="stern, keeps records",
        profile=profile,
        provider=provider,
        embedder=StubEmbeddingProvider(dimension=1024),
    )

    assert result.text == "You are a stern chronicler."
    assert provider.seen_purposes == ["rewrite"]

    async with tenant_scope(tenant_id) as session:
        purposes = [
            r.purpose
            for r in (
                await session.execute(
                    select(UsageRecordRow).where(UsageRecordRow.tenant_id == tenant_id)
                )
            ).scalars()
        ]
    assert "rewrite" in purposes


async def test_assist_rejects_unknown_task(db_available: None) -> None:
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=f"assist-task-{uuid.uuid4().hex[:8]}"
    )
    persona = await ensure_workspace_assistant(tenant_id, workspace_id)
    profile = await get_agent(tenant_id, persona.agent_id)
    assert profile is not None
    async with tenant_scope(tenant_id) as session:
        viewer = await session.get(Principal, owner_id)
        assert viewer is not None
        session.expunge(viewer)

    with pytest.raises(UnknownAssistTaskError):
        await assist(
            tenant_id,
            workspace_id,
            viewer,
            task="write_my_thesis",
            subject="",
            instruction="",
            profile=profile,
            provider=_ScriptedAssistProvider(text="", seen_purposes=[]),
            embedder=StubEmbeddingProvider(dimension=1024),
        )
