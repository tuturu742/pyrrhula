"""F3.12 acceptance criteria for chat-based knowledge editing, against a live Postgres
and a scripted `ModelProvider` double (mirroring `core.secrets.tests.test_drafting`'s own
pattern) -- no HTTP, no chat orchestration."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent
from core.agents.models import Agent
from core.audit.models import UsageRecordRow
from core.knowledge.authoring import (
    EntryFields,
    create_source,
    list_versions,
    publish_version,
    upsert_draft_entry,
)
from core.knowledge.diff import diff_versions
from core.knowledge.editing import (
    KnowledgeEditResult,
    apply_knowledge_edit_proposal,
    propose_knowledge_edit,
)
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


@dataclass
class _ScriptedEditProvider:
    result: KnowledgeEditResult

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
    tenant_id, owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    await upsert_draft_entry(
        tenant_id,
        source.id,
        "grappling",
        EntryFields(
            title="Grappling",
            body_md="old rules text",
            class_="rules",
            scope_key="workspace_public",
        ),
    )
    await publish_version(tenant_id, source.id)
    profile = await create_agent(
        tenant_id, "editing-test", "echo", "echo-1", encryptor=IdentityEncryptor()
    )
    return tenant_id, owner_id, source.id, profile


async def test_proposal_writes_new_version_only_on_approval(db_available: None) -> None:
    tenant_id, owner_id, source_id, profile = await _setup("edit-approve")
    provider = _ScriptedEditProvider(KnowledgeEditResult(body_md="new rules text"))

    proposal = await propose_knowledge_edit(
        tenant_id, source_id, "grappling", "shorten it", agent=profile, provider=provider
    )
    assert proposal.valid
    assert proposal.proposed_body_md == "new rules text"
    assert "-old rules text" in proposal.text_diff
    assert "+new rules text" in proposal.text_diff

    # Declining is simply never calling apply -- the version DAG is untouched.
    versions_before = await list_versions(tenant_id, source_id)
    assert len(versions_before) == 1

    published = await apply_knowledge_edit_proposal(
        tenant_id, source_id, "grappling", proposal.proposed_body_md, approved_by=owner_id
    )

    versions_after = await list_versions(tenant_id, source_id)
    assert len(versions_after) == 2
    assert published.ai_assisted is True
    assert published.created_by == owner_id

    diff = await diff_versions(tenant_id, versions_before[0].id, published.id)
    assert [c.entry_key for c in diff.changed] == ["grappling"]
    assert "-old rules text" in diff.changed[0].text_diff
    assert "+new rules text" in diff.changed[0].text_diff


async def test_accepted_edit_attributes_human_with_ai_assisted_marker(db_available: None) -> None:
    tenant_id, owner_id, source_id, profile = await _setup("edit-attribution")
    provider = _ScriptedEditProvider(KnowledgeEditResult(body_md="revised text"))

    proposal = await propose_knowledge_edit(
        tenant_id, source_id, "grappling", "revise", agent=profile, provider=provider
    )
    published = await apply_knowledge_edit_proposal(
        tenant_id, source_id, "grappling", proposal.proposed_body_md, approved_by=owner_id
    )

    assert published.created_by == owner_id
    assert published.ai_assisted is True

    # A manual publish (no proposal involved) stays ai_assisted=False by default --
    # the marker distinguishes provenance, it isn't just always-on for every version.
    await upsert_draft_entry(
        tenant_id,
        source_id,
        "grappling",
        EntryFields(
            title="Grappling", body_md="manual edit", class_="rules", scope_key="workspace_public"
        ),
    )
    manual = await publish_version(tenant_id, source_id, created_by=owner_id)
    assert manual.ai_assisted is False


async def test_proposal_call_is_metered_as_rewrite_regardless_of_approval(
    db_available: None,
) -> None:
    tenant_id, _owner_id, source_id, profile = await _setup("edit-metering")
    provider = _ScriptedEditProvider(KnowledgeEditResult(body_md="new text"))

    await propose_knowledge_edit(
        tenant_id, source_id, "grappling", "shorten it", agent=profile, provider=provider
    )

    async with tenant_scope(tenant_id) as session:
        row = (
            await session.execute(
                select(UsageRecordRow).where(UsageRecordRow.tenant_id == tenant_id)
            )
        ).scalar_one()
    assert row.purpose == "rewrite"
    assert row.agent_id == profile.id


async def test_empty_proposal_is_invalid_and_never_needs_to_be_applied(db_available: None) -> None:
    tenant_id, owner_id, source_id, profile = await _setup("edit-empty")
    provider = _ScriptedEditProvider(KnowledgeEditResult(body_md="   "))

    proposal = await propose_knowledge_edit(
        tenant_id, source_id, "grappling", "delete it", agent=profile, provider=provider
    )
    assert not proposal.valid
    assert any("empty" in issue for issue in proposal.issues)

    versions = await list_versions(tenant_id, source_id)
    assert len(versions) == 1
