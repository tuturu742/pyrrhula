"""Acceptance criteria for chat-based EntitySchema editing, against a live
Postgres and a scripted `ModelProvider` double (mirroring `core.secrets.tests.
test_drafting`'s pattern). This is the domain where "fails schema validation" has real
teeth (CEL compile-checking, FSM reachability, tag contracts) -- the acceptance
criterion this file proves."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import cast

import pytest
from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent
from core.agents.models import Agent
from core.audit.models import UsageRecordRow
from core.entities.editing import apply_schema_edit_proposal, propose_schema_edit
from core.entities.repo import save_schema
from core.entities.schema import DerivedDef, EntitySchemaDefinition, FieldDef
from core.entities.validation import SchemaValidationError
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest, ModelT
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


@dataclass
class _ScriptedEditProvider:
    result: EntitySchemaDefinition

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        raise NotImplementedError
        yield  # pragma: no cover -- makes this an async generator for the Protocol

    async def generate_structured(self, req: GenerationRequest, schema: type[ModelT]) -> ModelT:
        # A scripted double: always returns the pre-built `result`, whatever `schema`
        # asks for -- `cast` bridges that intentional shortcut to the Protocol's real
        # generic return type (mypy can't verify `schema is EntitySchemaDefinition` from
        # the dataclass shape alone; every call site in this file only ever asks for it).
        return cast(ModelT, self.result)

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
    definition = EntitySchemaDefinition(fields=[FieldDef(key="xp", type="integer", minimum=0)])
    await save_schema(tenant_id, workspace_id, "hero", 1, definition)
    profile = await create_agent(
        tenant_id, "editing-test", "echo", "echo-1", encryptor=IdentityEncryptor()
    )
    return tenant_id, owner_id, workspace_id, profile


async def test_invalid_proposals_are_filtered_before_presentation(db_available: None) -> None:
    tenant_id, owner_id, workspace_id, profile = await _setup("schema-edit-invalid")

    bad_definition = EntitySchemaDefinition(
        fields=[FieldDef(key="xp", type="integer", minimum=0)],
        derived=[DerivedDef(key="level", type="integer", expression="fields.nonexistent + 1")],
    )
    provider = _ScriptedEditProvider(bad_definition)

    proposal = await propose_schema_edit(
        tenant_id,
        workspace_id,
        "hero",
        "add a level field",
        agent=profile,
        provider=provider,
    )

    assert not proposal.valid
    assert any("derived[0].expression" in issue.field_path for issue in proposal.issues)
    assert any("nonexistent" in issue.message for issue in proposal.issues)

    # A caller (the API route / UI) must never present this for approval -- and even if
    # it tried to apply it anyway, save_schema's own independent validation still rejects
    # it (defense in depth: nothing invalid ever reaches a row).
    with pytest.raises(SchemaValidationError):
        await apply_schema_edit_proposal(
            tenant_id, workspace_id, "hero", proposal.proposed_definition, approved_by=owner_id
        )


async def test_valid_proposal_applies_as_new_version_attributed_to_approver(
    db_available: None,
) -> None:
    tenant_id, owner_id, workspace_id, profile = await _setup("schema-edit-valid")

    good_definition = EntitySchemaDefinition(
        fields=[FieldDef(key="xp", type="integer", minimum=0)],
        derived=[DerivedDef(key="level", type="integer", expression="fields.xp / 100 + 1")],
    )
    provider = _ScriptedEditProvider(good_definition)

    proposal = await propose_schema_edit(
        tenant_id,
        workspace_id,
        "hero",
        "add a level field",
        agent=profile,
        provider=provider,
    )
    assert proposal.valid
    assert proposal.diff.added_derived == ["level"]

    row = await apply_schema_edit_proposal(
        tenant_id, workspace_id, "hero", proposal.proposed_definition, approved_by=owner_id
    )
    assert row.version == 2
    assert row.created_by == owner_id
    assert row.ai_assisted is True

    async with tenant_scope(tenant_id) as session:
        usage_rows = (
            await session.execute(
                select(UsageRecordRow).where(UsageRecordRow.tenant_id == tenant_id)
            )
        ).scalars()
    assert all(r.purpose == "rewrite" for r in usage_rows)
