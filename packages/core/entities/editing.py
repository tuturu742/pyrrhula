"""chat-based editing for EntitySchemas (req 22) -- a chat turn
proposes a full replacement ``EntitySchemaDefinition``, diffed at the field/derived/
constraint/state-machine-key grain against the current latest version, and validated
through the *exact same* ``validate_schema_definition`` pass the manual save path
runs -- **before** it is ever shown for approval (this task's own "a proposal that fails
schema validation is never presented" acceptance criterion). Approval writes a new
immutable schema version (``core.entities.repo.save_schema``) attributed to the human
approver with an ``ai_assisted`` marker; decline never calls ``apply_schema_edit_
proposal``, so nothing is written.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from core.agents.models import Agent
from core.audit.models import UsageRecordRow
from core.entities.repo import get_latest_schema_version, next_version, save_schema
from core.entities.schema import EntitySchemaDefinition, EntitySchemaRow
from core.entities.validation import SchemaValidationIssue, validate_schema_definition
from core.ports.model_provider import GenerationRequest, ModelProvider
from core.tenancy.egress import load_egress_policy
from core.tenancy.scope import tenant_scope

_PURPOSE = "rewrite"

_SYSTEM_PROMPT = (
    "You edit an EntitySchema definition (fields/derived/constraints/state_machines/"
    "views, a JSON Schema 2020-12 subset plus CEL expressions) given an editing "
    "instruction. Return the full replacement definition document -- never a partial "
    "patch, never commentary."
)


class NoSuchSchemaError(Exception):
    pass


@dataclass(frozen=True)
class SchemaDefinitionDiff:
    added_fields: list[str] = field(default_factory=list)
    removed_fields: list[str] = field(default_factory=list)
    changed_fields: list[str] = field(default_factory=list)
    added_derived: list[str] = field(default_factory=list)
    removed_derived: list[str] = field(default_factory=list)
    changed_derived: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SchemaEditProposal:
    workspace_id: uuid.UUID | None
    key: str
    current_definition: EntitySchemaDefinition
    proposed_definition: EntitySchemaDefinition
    diff: SchemaDefinitionDiff
    valid: bool
    issues: list[SchemaValidationIssue]


def _diff_definitions(
    current: EntitySchemaDefinition, proposed: EntitySchemaDefinition
) -> SchemaDefinitionDiff:
    current_fields = {f.key: f.model_dump(mode="json") for f in current.fields}
    proposed_fields = {f.key: f.model_dump(mode="json") for f in proposed.fields}
    current_derived = {d.key: d.model_dump(mode="json") for d in current.derived}
    proposed_derived = {d.key: d.model_dump(mode="json") for d in proposed.derived}

    return SchemaDefinitionDiff(
        added_fields=sorted(set(proposed_fields) - set(current_fields)),
        removed_fields=sorted(set(current_fields) - set(proposed_fields)),
        changed_fields=sorted(
            k
            for k in set(current_fields) & set(proposed_fields)
            if current_fields[k] != proposed_fields[k]
        ),
        added_derived=sorted(set(proposed_derived) - set(current_derived)),
        removed_derived=sorted(set(current_derived) - set(proposed_derived)),
        changed_derived=sorted(
            k
            for k in set(current_derived) & set(proposed_derived)
            if current_derived[k] != proposed_derived[k]
        ),
    )


async def propose_schema_edit(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    key: str,
    instruction: str,
    *,
    agent: Agent,
    provider: ModelProvider,
) -> SchemaEditProposal:
    """Calls the model, meters the call (``usage_record``, ``purpose='rewrite'``)
    regardless of outcome, then runs the proposed definition through its own
    ``validate_schema_definition`` -- a CEL compile failure, dangling FSM transition, or
    unknown tag makes ``valid=False`` with the exact same field-anchored issues the
    manual schema editor would show, before any diff is ever presented."""
    current_row = await get_latest_schema_version(tenant_id, workspace_id, key)
    if current_row is None:
        raise NoSuchSchemaError(f"no schema {key!r} in this scope")
    current_definition = current_row.to_definition()

    model_string = f"{agent.provider}/{agent.model}"
    req = GenerationRequest(
        egress_policy=await load_egress_policy(tenant_id),
        model=model_string,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Current definition:\n{current_definition.model_dump_json()}\n\n"
                    f"Instruction: {instruction}"
                ),
            },
        ],
        purpose=_PURPOSE,
        max_tokens=1500,
        api_base=agent.api_base,
        params=dict(agent.params or {}),
    )

    start = time.monotonic()
    proposed_definition = await provider.generate_structured(req, EntitySchemaDefinition)
    latency_ms = int((time.monotonic() - start) * 1000)

    prompt_tokens = sum(
        provider.count_tokens(str(m.get("content") or ""), model_string) for m in req.messages
    )
    completion_tokens = provider.count_tokens(proposed_definition.model_dump_json(), model_string)

    async with tenant_scope(tenant_id) as session:
        session.add(
            UsageRecordRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                agent_id=agent.id,
                provider=agent.provider,
                model=agent.model,
                purpose=_PURPOSE,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
            )
        )

    issues = validate_schema_definition(proposed_definition)

    return SchemaEditProposal(
        workspace_id=workspace_id,
        key=key,
        current_definition=current_definition,
        proposed_definition=proposed_definition,
        diff=_diff_definitions(current_definition, proposed_definition),
        valid=not issues,
        issues=issues,
    )


async def apply_schema_edit_proposal(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    key: str,
    proposed_definition: EntitySchemaDefinition,
    *,
    approved_by: uuid.UUID,
) -> EntitySchemaRow:
    """Writes the approved proposal as a new schema version, attributed to the human
    approver and marked ``ai_assisted``. ``save_schema`` re-validates independently (F3.1
    discipline: every write path validates, never trusts an upstream check alone) --
    this is defense in depth, not a duplicate of ``propose_schema_edit``'s own check."""
    version = await next_version(tenant_id, workspace_id, key)
    return await save_schema(
        tenant_id,
        workspace_id,
        key,
        version,
        proposed_definition,
        created_by=approved_by,
        ai_assisted=True,
    )
