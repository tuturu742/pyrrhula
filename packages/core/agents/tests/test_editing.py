"""Acceptance criteria for chat-based agent-persona editing, against a live
Postgres and a scripted `ModelProvider` double (mirroring `core.secrets.tests.
test_drafting`'s pattern) -- the third of the three proposal targets."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import (
    create_agent,
    create_persona,
    get_persona,
    list_persona_versions,
)
from core.agents.editing import (
    PersonaEditResult,
    apply_persona_edit_proposal,
    propose_persona_edit,
)
from core.agents.models import Agent
from core.audit.models import UsageRecordRow
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


@dataclass
class _ScriptedEditProvider:
    result: PersonaEditResult

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        raise NotImplementedError
        yield  # pragma: no cover -- makes this an async generator for the Protocol

    async def generate_structured(self, req: GenerationRequest, schema: type) -> object:  # type: ignore[type-arg]
        return self.result

    def count_tokens(self, text: str, model: str) -> int:
        return max(len(text.split()), 1)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            supports_tools=False, supports_json_mode=True, supports_prompt_caching=False
        )


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, Agent]:
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    profile = await create_agent(
        tenant_id, "editing-test", "echo", "echo-1", encryptor=IdentityEncryptor()
    )
    agent = await create_persona(
        tenant_id,
        workspace_id,
        "narrator",
        "Narrator",
        profile.id,
        persona_md="speaks in short, blunt sentences",
    )
    return tenant_id, owner_id, agent.id, profile


async def test_proposal_writes_persona_version_only_on_approval(db_available: None) -> None:
    tenant_id, owner_id, persona_id, profile = await _setup("persona-approve")
    provider = _ScriptedEditProvider(
        PersonaEditResult(persona_md="speaks in flowery, ornate prose")
    )

    proposal = await propose_persona_edit(
        tenant_id, persona_id, "make them more flowery", agent=profile, provider=provider
    )
    assert proposal.valid
    assert "-speaks in short, blunt sentences" in proposal.text_diff
    assert "+speaks in flowery, ornate prose" in proposal.text_diff

    # Declining is simply never calling apply.
    assert (await list_persona_versions(tenant_id, persona_id)) == []
    unchanged = await get_persona(tenant_id, persona_id)
    assert unchanged is not None
    assert unchanged.persona_md == "speaks in short, blunt sentences"

    await apply_persona_edit_proposal(
        tenant_id, persona_id, proposal.proposed_persona_md, approved_by=owner_id
    )

    versions = await list_persona_versions(tenant_id, persona_id)
    assert len(versions) == 1
    assert versions[0].ai_assisted is True
    assert versions[0].created_by == owner_id

    updated = await get_persona(tenant_id, persona_id)
    assert updated is not None
    assert updated.persona_md == "speaks in flowery, ornate prose"


async def test_persona_proposal_call_is_metered_as_rewrite(db_available: None) -> None:
    tenant_id, _owner_id, persona_id, profile = await _setup("persona-metering")
    provider = _ScriptedEditProvider(PersonaEditResult(persona_md="new persona"))

    await propose_persona_edit(tenant_id, persona_id, "rewrite", agent=profile, provider=provider)

    async with tenant_scope(tenant_id) as session:
        row = (
            await session.execute(
                select(UsageRecordRow).where(UsageRecordRow.tenant_id == tenant_id)
            )
        ).scalar_one()
    assert row.purpose == "rewrite"


async def test_empty_persona_proposal_is_invalid(db_available: None) -> None:
    tenant_id, _owner_id, persona_id, profile = await _setup("persona-empty")
    provider = _ScriptedEditProvider(PersonaEditResult(persona_md=""))

    proposal = await propose_persona_edit(
        tenant_id, persona_id, "clear it", agent=profile, provider=provider
    )
    assert not proposal.valid
    assert any("empty" in issue for issue in proposal.issues)
