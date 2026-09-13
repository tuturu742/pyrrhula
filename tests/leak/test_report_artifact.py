"""G4.10's leak criterion: a participant recap generated over a session containing
concealed secrets contains no trace of them. Scanned at the artifact level -- the report's
own `content_md`, plus its stored row -- with the same three detectors G4.7 uses.

The point of scanning the artifact rather than the code path is the same here as there:
the property a reader relies on is about the words in the recap. A code-path test proves
that today's pipeline calls the filter; this proves the recap does not contain the fact,
whatever the pipeline grows into.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from adapters.moderation.allow_all import AllowAllModerationProvider
from adapters.permission.role_permission import RolePermissionService
from core.agents.authoring import create_agent
from core.agents.seed import seed_dev_agent
from core.assembler.visibility import seed_default_scopes
from core.entities.mutation import mutate
from core.entities.repo import save_schema
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import create_entity
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest, ModelT
from core.process.dsl.schema import ActorSpec, BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.skeleton import create_session
from core.reporting.pipeline import ReportRow, generate_report
from core.reporting.templates import get_template
from core.secrets.authoring import create_secret
from core.sessions.models import SessionEventRow
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant
from tests.leak.test_export_modes import _embedding_hit, _exact_hit, _fuzzy_hit

_PERMISSIONS = RolePermissionService()

_CANARY = "the steward sold the granary keys to the river guild before the siege began"
_HIDDEN_ENTITY_NAME = "The Steward's Ledger"


@dataclass
class _LeakyProvider:
    """A model that repeats the secret when it is given it. If the pipeline's filter works,
    it is never given it -- so this double's *willingness* to leak is what makes the test
    meaningful rather than a coincidence of a well-behaved model."""

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        raise NotImplementedError
        yield  # pragma: no cover -- makes this an async generator for the Protocol

    async def generate_structured(self, req: GenerationRequest, schema: type[ModelT]) -> ModelT:
        joined = "\n".join(m["content"] for m in req.messages)
        return schema.model_validate(dict.fromkeys(schema.model_fields, joined))

    def count_tokens(self, text: str, model: str) -> int:
        return max(len(text.split()), 1)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            supports_tools=False, supports_json_mode=True, supports_prompt_caching=False
        )


def scan_text_blob(blob: str, secret: str) -> list[str]:
    return [
        name
        for name, fn in (
            ("exact", _exact_hit),
            ("fuzzy", _fuzzy_hit),
            ("embedding", _embedding_hit),
        )
        if fn(secret, blob)
    ]


def _phase() -> PhaseSpec:
    return PhaseSpec(
        label_key="turn",
        actors=[ActorSpec(persona_type="supervisor", mode="generate")],
        visibility=VisibilitySpec(
            knowledge_classes=[],
            scopes=["workspace_public"],
            entity_fields="all",
            secrets="none",
        ),
        budget=BudgetSpec(ratio={}, max_tokens=400),
    )


async def test_participant_report_contains_no_unheld_secret_content(
    db_available: None,
) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"report-leak-{uuid.uuid4().hex[:8]}"
    )
    await seed_default_scopes(tenant_id, workspace_id)

    async with tenant_scope(tenant_id) as session:
        author = Principal(tenant_id=tenant_id, kind="human", display_name="author")
        participant = Principal(tenant_id=tenant_id, kind="human", display_name="participant")
        session.add_all([author, participant])
        await session.flush()
        for principal, role in ((author, "facilitator"), (participant, "participant")):
            session.add(
                WorkspaceMembership(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    principal_id=principal.id,
                    role=role,
                )
            )
        await session.flush()
        session.expunge(author)
        session.expunge(participant)

    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, f"leak-{uuid.uuid4().hex[:6]}", "echo", "echo-1", encryptor=IdentityEncryptor()
    )

    await create_secret(
        tenant_id,
        workspace_id,
        author.id,
        subject_kind="entity",
        subject_id=uuid.uuid4(),
        content=_CANARY,
        gist="the steward did something",
        scope_key="facilitator_only",
        encryptor=IdentityEncryptor(),
        permission_service=_PERMISSIONS,
        moderation_provider=AllowAllModerationProvider(),
    )

    # A facilitator-only entity whose *name* is itself a giveaway, mutated in-session so it
    # would land in the fact frame of anyone who could see it.
    definition = EntitySchemaDefinition(fields=[FieldDef(key="pages", type="integer")])
    schema_row = await save_schema(tenant_id, workspace_id, "ledger", 1, definition)
    hidden = await create_entity(
        tenant_id,
        workspace_id,
        schema_row.id,
        definition,
        key="stewards-ledger",
        name=_HIDDEN_ENTITY_NAME,
        scope_key="facilitator_only",
        data={"pages": 1},
    )
    await mutate(
        author.id,
        tenant_id,
        workspace_id,
        hidden.id,
        {"pages": 12},
        "human",
        None,
        f"leak-{hidden.id}",
        permission_service=_PERMISSIONS,
        session_id=sess.id,
        event_seq=3,
    )

    async with tenant_scope(tenant_id) as session:
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=sess.id,
                event_seq=1,
                kind="message",
                payload={"phase": "turn", "content": "The siege held another day."},
            )
        )

    result = await generate_report(
        tenant_id,
        workspace_id,
        sess.id,
        participant,
        _phase(),
        get_template("campaign_recap"),
        from_event_seq=0,
        to_event_seq=10,
        agent=profile,
        provider=_LeakyProvider(),
        permission_service=_PERMISSIONS,
    )

    hits = scan_text_blob(result.content_md, _CANARY)
    assert hits == [], (
        f"a participant recap leaked a concealed secret: detectors {hits} fired. The "
        "pipeline filters the input stream before any model call -- never present, not "
        "scrubbed after."
    )
    assert "stewards-ledger" not in result.content_md
    assert _HIDDEN_ENTITY_NAME not in result.content_md

    # And the stored row is clean too -- a report that scrubbed only on the way out would
    # still be a leak sitting in the database.
    async with tenant_scope(tenant_id) as session:
        row = await session.get(ReportRow, result.report_id)
        assert row is not None
        stored = row.content_md
    assert scan_text_blob(stored, _CANARY) == []

    # The facilitator, who can see the compartment, does get the entity change -- proving
    # the recap was filtered rather than merely empty.
    facilitator_report = await generate_report(
        tenant_id,
        workspace_id,
        sess.id,
        author,
        PhaseSpec(
            label_key="turn",
            actors=[ActorSpec(persona_type="supervisor", mode="generate")],
            visibility=VisibilitySpec(
                knowledge_classes=[],
                scopes=["workspace_public", "facilitator_only"],
                entity_fields="all",
                secrets="none",
            ),
            budget=BudgetSpec(ratio={}, max_tokens=400),
        ),
        get_template("session_log"),
        from_event_seq=0,
        to_event_seq=10,
        agent=profile,
        provider=_LeakyProvider(),
        permission_service=_PERMISSIONS,
    )
    assert "stewards-ledger" in facilitator_report.content_md, (
        "the fixture proves nothing: nobody could see the hidden entity, so filtering it "
        "out of the participant's recap was not a filter doing work"
    )

    async with tenant_scope(tenant_id) as session:
        count = len(
            (await session.execute(select(ReportRow.id).where(ReportRow.session_id == sess.id)))
            .scalars()
            .all()
        )
    assert count == 2
