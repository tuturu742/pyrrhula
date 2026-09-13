"""E2.2's AI-assist acceptance criteria for `core.secrets.drafting` in isolation, against
a scripted `ModelProvider` double -- no HTTP, no `core.secrets.authoring` involved."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent
from core.agents.models import Agent
from core.audit.models import UsageRecordRow
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest
from core.secrets.drafting import (
    DraftContainsPlaintextError,
    SecretDraftResult,
    draft_directive_and_hint,
)
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


@dataclass
class _ScriptedDraftProvider:
    result: SecretDraftResult

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


async def _seed(slug_prefix: str) -> tuple[uuid.UUID, Agent]:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    profile = await create_agent(
        tenant_id, "drafting-test", "echo", "echo-1", encryptor=IdentityEncryptor()
    )
    return tenant_id, profile


async def test_clean_draft_is_returned_and_metered_as_rewrite(db_available: None) -> None:
    tenant_id, profile = await _seed("draft-clean")
    provider = _ScriptedDraftProvider(
        SecretDraftResult(
            behavioral_directive="grows quiet whenever the topic comes up",
            hint_text="something about the old bridge",
        )
    )

    draft = await draft_directive_and_hint(
        tenant_id, "the bridge collapse was sabotage", agent=profile, provider=provider
    )
    assert draft.behavioral_directive == "grows quiet whenever the topic comes up"

    async with tenant_scope(tenant_id) as session:
        row = (
            await session.execute(
                select(UsageRecordRow).where(UsageRecordRow.tenant_id == tenant_id)
            )
        ).scalar_one()
    assert row.purpose == "rewrite"
    assert row.agent_id == profile.id
    assert row.prompt_tokens > 0
    assert row.completion_tokens > 0


async def test_draft_leaking_plaintext_verbatim_is_rejected_but_still_metered(
    db_available: None,
) -> None:
    tenant_id, profile = await _seed("draft-leak")
    content = "the vault combination is thirty one seventeen forty two written on a card"
    provider = _ScriptedDraftProvider(
        SecretDraftResult(
            # Echoes a long run of `content` verbatim -- exactly what must be caught.
            behavioral_directive="never says the vault combination is thirty one seventeen",
            hint_text="something locked away",
        )
    )

    with pytest.raises(DraftContainsPlaintextError):
        await draft_directive_and_hint(tenant_id, content, agent=profile, provider=provider)

    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(UsageRecordRow).where(UsageRecordRow.tenant_id == tenant_id)
            )
        ).scalars()
    assert len(list(rows)) == 1, "the call still cost money and must still be metered"


async def test_paraphrased_draft_with_no_shared_run_of_words_is_accepted(
    db_available: None,
) -> None:
    tenant_id, profile = await _seed("draft-safe")
    provider = _ScriptedDraftProvider(
        SecretDraftResult(
            behavioral_directive="avoids eye contact near the riverbank",
            hint_text="an old structural failure",
        )
    )

    draft = await draft_directive_and_hint(
        tenant_id,
        "the bridge collapse was sabotage by a rival contractor",
        agent=profile,
        provider=provider,
    )
    assert "sabotage" not in draft.behavioral_directive
