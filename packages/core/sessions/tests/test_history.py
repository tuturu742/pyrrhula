"""G4.1 acceptance criteria for elapsed-history summarisation, against a live Postgres and
a scripted ``ModelProvider`` double (the same pattern ``core.knowledge.tests.test_editing``
and ``tests/isolation/test_entity_editing`` use).

The scripted double is not incidental here -- it is the instrument. Both of this file's
tests work by making the *model* say something the records contradict, and then asserting
that what the model said had no purchase on the fact frame. A real provider could not be
relied on to lie on cue.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from adapters.permission.role_permission import RolePermissionService
from core.agents.authoring import create_agent
from core.assembler.visibility import seed_default_scopes
from core.entities.mutation import mutate
from core.entities.repo import save_schema
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import create_entity
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest, ModelT
from core.process.dsl.schema import ActorSpec, BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.skeleton import create_session
from core.resolution.rule_system import (
    RuleSystemDefinition,
    get_or_create_default_rule_system,
)
from core.resolution.service import resolve
from core.sessions.history import summarise_history
from core.sessions.models import SessionEventRow
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

from adapters.encryptor.identity import IdentityEncryptor  # isort: skip
from core.agents.seed import seed_dev_agent  # isort: skip

_PERMISSIONS = RolePermissionService()


def _phase(scopes: list[str]) -> PhaseSpec:
    return PhaseSpec(
        label_key="turn",
        actors=[ActorSpec(persona_type="supervisor", mode="generate")],
        visibility=VisibilitySpec(
            knowledge_classes=[], scopes=scopes, entity_fields="all", secrets="none"
        ),
        budget=BudgetSpec(ratio={}, max_tokens=400, history_ratio=0.5),
    )


@dataclass
class _ScriptedSummaryProvider:
    """Returns ``line`` for every structured call, whatever schema is asked for -- both
    the per-phase map step and the reduce step. ``cast`` bridges that intentional
    shortcut to the Protocol's real generic return type."""

    line: str

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        raise NotImplementedError
        yield  # pragma: no cover -- makes this an async generator for the Protocol

    async def generate_structured(self, req: GenerationRequest, schema: type[ModelT]) -> ModelT:
        return schema.model_validate(dict.fromkeys(schema.model_fields, self.line))

    def count_tokens(self, text: str, model: str) -> int:
        return max(len(text.split()), 1)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            supports_tools=False, supports_json_mode=True, supports_prompt_caching=False
        )


async def _add_principal(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, role: str, name: str
) -> Principal:
    async with tenant_scope(tenant_id) as session:
        principal = Principal(tenant_id=tenant_id, kind="human", display_name=name)
        session.add(principal)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal.id,
                role=role,
            )
        )
        await session.flush()
        session.expunge(principal)
        return principal


async def _log_message(
    tenant_id: uuid.UUID, session_id: uuid.UUID, event_seq: int, phase: str, content: str
) -> None:
    async with tenant_scope(tenant_id) as session:
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                kind="message",
                payload={"phase": phase, "content": content},
            )
        )


async def test_summary_takes_mechanical_facts_from_records_not_prose(
    db_available: None,
) -> None:
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=f"history-facts-{uuid.uuid4().hex[:8]}"
    )
    await seed_default_scopes(tenant_id, workspace_id)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    viewer = await _add_principal(tenant_id, workspace_id, "facilitator", "resumer")
    profile = await create_agent(
        tenant_id, "history-test", "echo", "echo-1", encryptor=IdentityEncryptor()
    )

    rule_row = await get_or_create_default_rule_system(tenant_id)
    record = await resolve(
        tenant_id=tenant_id,
        session_id=sess.id,
        event_seq=2,
        tool_key="randomizer",
        actor_entity_id=None,
        expression="1d20",
        check_type="stealth",
        actor_fields={"dexterity": 14},
        target=15,
        rule_system=RuleSystemDefinition.from_row(rule_row),
        rule_system_id=rule_row.id,
        legal_check_types=frozenset({"stealth"}),
    )

    # The planted lie: prose in the elapsed history claiming a result the record refutes.
    await _log_message(tenant_id, sess.id, 1, "turn", "We crept toward the gate.")
    await _log_message(
        tenant_id,
        sess.id,
        3,
        "turn",
        "The roll came up a natural 20 for a total of 99 -- a flawless success!",
    )

    # ...and a provider that faithfully repeats it, so the only thing standing between the
    # lie and the summary is that the fact frame never went through the model at all.
    provider = _ScriptedSummaryProvider("The roll came up a natural 20, total 99, a success.")

    summary = await summarise_history(
        tenant_id,
        workspace_id,
        sess.id,
        viewer,
        _phase(["workspace_public"]),
        from_event_seq=0,
        to_event_seq=10,
        max_tokens=200,
        agent=profile,
        provider=provider,
        permission_service=_PERMISSIONS,
    )

    resolutions = [f for f in summary.facts if f.kind == "resolution"]
    assert len(resolutions) == 1
    assert resolutions[0].fields["total"] == record.total
    assert resolutions[0].fields["outcome"] == record.outcome
    assert f"total={record.total}" in summary.rendered_text
    assert f"outcome={record.outcome}" in summary.rendered_text
    # The narrative may repeat the lie -- it is the model's prose and is labelled as such.
    # What must not happen is the lie becoming the *recorded* value.
    assert record.total != 99
    assert summary.rendered_text.index("[recorded facts]") < summary.rendered_text.index(
        "[narrative]"
    )

    del owner_id


async def test_history_summary_is_visibility_filtered_per_viewer(db_available: None) -> None:
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=f"history-vis-{uuid.uuid4().hex[:8]}"
    )
    await seed_default_scopes(tenant_id, workspace_id)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, "history-vis", "echo", "echo-1", encryptor=IdentityEncryptor()
    )

    facilitator = await _add_principal(tenant_id, workspace_id, "facilitator", "director")
    participant = await _add_principal(tenant_id, workspace_id, "participant", "player")

    definition = EntitySchemaDefinition(fields=[FieldDef(key="counter", type="integer")])
    schema_row = await save_schema(tenant_id, workspace_id, "thing", 1, definition)

    public = await create_entity(
        tenant_id,
        workspace_id,
        schema_row.id,
        definition,
        key=f"public-{uuid.uuid4().hex[:8]}",
        name="Public Thing",
        scope_key="workspace_public",
        data={"counter": 1},
    )
    hidden = await create_entity(
        tenant_id,
        workspace_id,
        schema_row.id,
        definition,
        key=f"hidden-{uuid.uuid4().hex[:8]}",
        name="Hidden Thing",
        scope_key="facilitator_only",
        data={"counter": 1},
    )

    for entity in (public, hidden):
        await mutate(
            facilitator.id,
            tenant_id,
            workspace_id,
            entity.id,
            {"counter": 7},
            "human",
            None,
            f"vis-{entity.id}",
            permission_service=_PERMISSIONS,
            session_id=sess.id,
            event_seq=4,
        )

    provider = _ScriptedSummaryProvider("Some things happened.")
    phase = _phase(["workspace_public", "facilitator_only"])

    async def summarise_for(viewer: Principal) -> set[str]:
        summary = await summarise_history(
            tenant_id,
            workspace_id,
            sess.id,
            viewer,
            phase,
            from_event_seq=0,
            to_event_seq=10,
            max_tokens=200,
            agent=profile,
            provider=provider,
            permission_service=_PERMISSIONS,
        )
        return {str(f.fields["entity"]) for f in summary.facts if f.kind == "entity_change"}

    facilitator_entities = await summarise_for(facilitator)
    participant_entities = await summarise_for(participant)

    assert public.key in facilitator_entities
    assert hidden.key in facilitator_entities
    assert public.key in participant_entities
    assert hidden.key not in participant_entities, (
        "a participant's recap contained a facilitator_only entity's state change -- the "
        "summary is not visibility-filtered"
    )

    del owner_id
