"""G4.10 acceptance criteria for the report pipeline: a participant recap carries no trace
of a concealed secret, a planted prose lie cannot reach the fact frame, redactions render
as visible stubs, and the recorded provenance regenerates an identical frame.

The leak-scan half of criterion 1 lives in ``tests/leak/test_report_artifact.py`` -- same
artifact-level method as G4.7, same reason.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import select, text

from adapters.encryptor.identity import IdentityEncryptor
from adapters.permission.role_permission import RolePermissionService
from core.agents.authoring import create_agent
from core.agents.seed import seed_dev_agent
from core.assembler.visibility import seed_default_scopes
from core.audit.models import UsageRecordRow
from core.entities.mutation import mutate
from core.entities.repo import save_schema
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import create_entity
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest, ModelT
from core.process.dsl.schema import ActorSpec, BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.skeleton import create_session
from core.reporting.pipeline import (
    ReportRow,
    generate_report,
    regenerate_fact_frame,
    render_redaction_stub,
)
from core.reporting.templates import (
    BUILT_IN_TEMPLATES,
    ReportTemplate,
    Step,
    get_template,
)
from core.resolution.rule_system import RuleSystemDefinition, get_or_create_default_rule_system
from core.resolution.service import resolve
from core.sessions.models import SessionEventRow
from core.tenancy.models import Principal, Workspace, WorkspaceMembership
from core.tenancy.scope import tenant_scope

_PERMISSIONS = RolePermissionService()


@dataclass
class _ScriptedProvider:
    """Echoes ``line`` for every step. The instrument, not an accident: both of the first
    two criteria work by making the model say something the records contradict."""

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


def _phase(scopes: list[str]) -> PhaseSpec:
    return PhaseSpec(
        label_key="turn",
        actors=[ActorSpec(persona_type="supervisor", mode="generate")],
        visibility=VisibilitySpec(
            knowledge_classes=[], scopes=scopes, entity_fields="all", secrets="none"
        ),
        budget=BudgetSpec(ratio={}, max_tokens=400),
    )


async def _workspace_of(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _member(tenant_id: uuid.UUID, workspace_id: uuid.UUID, role: str) -> Principal:
    async with tenant_scope(tenant_id) as session:
        principal = Principal(tenant_id=tenant_id, kind="human", display_name=role)
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
    tenant_id: uuid.UUID, session_id: uuid.UUID, event_seq: int, content: str
) -> None:
    async with tenant_scope(tenant_id) as session:
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                kind="message",
                payload={"phase": "turn", "content": content},
            )
        )


# ── template validation: the rule is structural, not a convention ───────────────────


def test_a_template_without_a_fact_frame_step_is_rejected() -> None:
    with pytest.raises(ValueError, match="fact_frame"):
        ReportTemplate(
            key="prose_only",
            label_key="report.prose",
            audience_mode="participant",
            pipeline=[Step(kind="reduce"), Step(kind="render")],
        )
    with pytest.raises(ValueError, match="first step"):
        ReportTemplate(
            key="late_frame",
            label_key="report.late",
            audience_mode="participant",
            pipeline=[Step(kind="reduce"), Step(kind="fact_frame")],
        )
    # The rule is structural, so assert it of every built-in rather than pinning the set:
    # a hardcoded roster only records which templates existed the day it was written, and
    # this one went red when `composed_document` was added rather than catching anything.
    assert {"narrative_recap", "session_log", "decision_summary"} <= set(BUILT_IN_TEMPLATES)
    for key, template in BUILT_IN_TEMPLATES.items():
        assert template.pipeline[0].kind == "fact_frame", (
            f"{key} must derive from the record before it renders anything"
        )


# ── acceptance criteria ─────────────────────────────────────────────────────────────


async def test_report_filter_omission(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """CLAUDE.md rule 4: `report` is a new tenant-scoped RLS table, so its filter-omission
    test lands in the same PR."""
    tenant_a, tenant_b = two_tenants
    for tenant_id in (tenant_a, tenant_b):
        workspace_id = await _workspace_of(tenant_id)
        await seed_default_scopes(tenant_id, workspace_id)
        viewer = await _member(tenant_id, workspace_id, "facilitator")
        persona_id = await seed_dev_agent(tenant_id, workspace_id)
        sess = await create_session(tenant_id, workspace_id, persona_id)
        profile = await create_agent(
            tenant_id,
            f"iso-{uuid.uuid4().hex[:6]}",
            "echo",
            "echo-1",
            encryptor=IdentityEncryptor(),
        )
        await generate_report(
            tenant_id,
            workspace_id,
            sess.id,
            viewer,
            _phase(["workspace_public"]),
            get_template("session_log"),
            from_event_seq=0,
            to_event_seq=10,
            agent=profile,
            provider=_ScriptedProvider("unused"),
            permission_service=_PERMISSIONS,
        )

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM report"))).all()

    assert {row[0] for row in rows} == {tenant_a}


async def test_report_facts_come_from_records_not_prose(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    viewer = await _member(tenant_id, workspace_id, "facilitator")
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, f"rep-{uuid.uuid4().hex[:6]}", "echo", "echo-1", encryptor=IdentityEncryptor()
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

    await _log_message(tenant_id, sess.id, 1, "We crept toward the gate.")
    await _log_message(
        tenant_id, sess.id, 3, "The roll was a natural 20 for a total of 99 -- flawless."
    )

    result = await generate_report(
        tenant_id,
        workspace_id,
        sess.id,
        viewer,
        _phase(["workspace_public"]),
        get_template("narrative_recap"),
        from_event_seq=0,
        to_event_seq=10,
        agent=profile,
        provider=_ScriptedProvider("The roll was a natural 20, total 99, flawless."),
        permission_service=_PERMISSIONS,
    )

    resolutions = [f for f in result.fact_frame.facts if f.kind == "resolution"]
    assert len(resolutions) == 1
    assert resolutions[0].fields["total"] == record.total
    assert record.total != 99

    # The rendered report states the *recorded* value, and states it before the narrative:
    # a reader who stops after the first section has read the part that is true by
    # construction.
    assert f"total={record.total}" in result.content_md
    assert result.content_md.index("## Recorded facts") < result.content_md.index("## Narrative")

    # Metering rides the report purpose, not a new one.
    async with tenant_scope(tenant_id) as session:
        purposes = set(
            (
                await session.execute(
                    select(UsageRecordRow.purpose).where(UsageRecordRow.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
    assert purposes == {"report"}


async def test_redactions_are_visible_stubs_not_silent_omissions(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    viewer = await _member(tenant_id, workspace_id, "participant")
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, f"red-{uuid.uuid4().hex[:6]}", "echo", "echo-1", encryptor=IdentityEncryptor()
    )

    for seq in range(3):
        await _log_message(tenant_id, sess.id, seq, f"message {seq}")

    # A sanitised report drops prose entirely -- prose is where a paraphrase of a concealed
    # fact would live -- so all three messages become an acknowledged omission.
    sanitised = ReportTemplate(
        key="sanitised_log",
        label_key="report.sanitised",
        audience_mode="sanitised",
        pipeline=[Step(kind="fact_frame"), Step(kind="render")],
    )
    result = await generate_report(
        tenant_id,
        workspace_id,
        sess.id,
        viewer,
        _phase(["workspace_public"]),
        sanitised,
        from_event_seq=0,
        to_event_seq=10,
        agent=profile,
        provider=_ScriptedProvider("unused"),
        permission_service=_PERMISSIONS,
    )

    assert result.redactions == [{"type": "event", "count": 3, "reason": "not visible to you"}]
    assert render_redaction_stub(3) in result.content_md, (
        "the omission was silent -- a reader would mistake an incomplete report for a complete one"
    )
    assert "message 0" not in result.content_md

    async with tenant_scope(tenant_id) as session:
        row = await session.get(ReportRow, result.report_id)
        assert row is not None
        assert row.redactions == result.redactions
        assert row.audience_mode == "sanitised"
        assert row.generated_for_principal_id == viewer.id

    assert render_redaction_stub(1).endswith("event not visible to you]_")


async def test_report_provenance_regenerates_identical_fact_frame(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    viewer = await _member(tenant_id, workspace_id, "facilitator")
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, f"prov-{uuid.uuid4().hex[:6]}", "echo", "echo-1", encryptor=IdentityEncryptor()
    )

    definition = EntitySchemaDefinition(fields=[FieldDef(key="counter", type="integer")])
    schema_row = await save_schema(tenant_id, workspace_id, "thing", 1, definition)
    entity = await create_entity(
        tenant_id,
        workspace_id,
        schema_row.id,
        definition,
        key=f"thing-{uuid.uuid4().hex[:8]}",
        name="Thing",
        scope_key="workspace_public",
        data={"counter": 1},
    )
    await mutate(
        viewer.id,
        tenant_id,
        workspace_id,
        entity.id,
        {"counter": 9},
        "human",
        None,
        f"prov-{entity.id}",
        permission_service=_PERMISSIONS,
        session_id=sess.id,
        event_seq=4,
    )

    template = get_template("session_log")
    result = await generate_report(
        tenant_id,
        workspace_id,
        sess.id,
        viewer,
        _phase(["workspace_public"]),
        template,
        from_event_seq=0,
        to_event_seq=10,
        agent=profile,
        provider=_ScriptedProvider("unused"),
        permission_service=_PERMISSIONS,
    )
    assert result.fact_frame.facts

    async with tenant_scope(tenant_id) as session:
        row = await session.get(ReportRow, result.report_id)
        assert row is not None
        assert row.source_event_from == 0
        assert row.source_event_to == 10
        assert row.fact_frame_hash == result.fact_frame.content_hash
        session.expunge(row)

    rebuilt = await regenerate_fact_frame(
        tenant_id,
        workspace_id,
        row,
        viewer,
        _phase(["workspace_public"]),
        template,
        permission_service=_PERMISSIONS,
    )
    assert rebuilt.content_hash == row.fact_frame_hash, (
        "the recorded provenance did not regenerate the same fact frame -- which is only "
        "possible if the frame came from something other than records"
    )
    assert rebuilt.rendered == result.fact_frame.rendered


async def test_decision_summary_selects_only_decision_shaped_facts(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Not a named criterion, but the `decision_summary` template's whole point: the
    artefact §11.5 says competitors cannot produce is a *decision* log, and one padded
    with every dice roll in the session buries the thing it exists for."""
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    viewer = await _member(tenant_id, workspace_id, "overseer")
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, f"dec-{uuid.uuid4().hex[:6]}", "echo", "echo-1", encryptor=IdentityEncryptor()
    )

    rule_row = await get_or_create_default_rule_system(tenant_id)
    await resolve(
        tenant_id=tenant_id,
        session_id=sess.id,
        event_seq=1,
        tool_key="randomizer",
        actor_entity_id=None,
        expression="1d20",
        check_type="stealth",
        actor_fields={"dexterity": 14},
        target=12,
        rule_system=RuleSystemDefinition.from_row(rule_row),
        rule_system_id=rule_row.id,
        legal_check_types=frozenset({"stealth"}),
    )

    template = get_template("decision_summary")
    assert template.requires_review is True

    result = await generate_report(
        tenant_id,
        workspace_id,
        sess.id,
        viewer,
        _phase(["workspace_public"]),
        template,
        from_event_seq=0,
        to_event_seq=10,
        agent=profile,
        provider=_ScriptedProvider("A decision was reached."),
        permission_service=_PERMISSIONS,
    )

    kinds = {f.kind for f in result.fact_frame.facts}
    assert "resolution" not in kinds, (
        "the decision summary included dice rolls -- its fact_kinds filter did nothing"
    )
