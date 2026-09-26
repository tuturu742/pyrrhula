"""the acceptance criteria for context exclusion (`core.assembler.context_assembler`
step 8 + `core.secrets.exclusion`): a concealed secret's plaintext is structurally absent
from the assembled context, concealment injects the behavioral directive in its place,
and a reveal commits the disclosure event + holder update atomically.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from core.agents.seed import seed_dev_agent
from core.assembler.context_assembler import assemble
from core.knowledge.retrieval.tests.conftest import unit_vector
from core.process.dsl.schema import ActorSpec, BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.skeleton import create_session
from core.secrets.exclusion import ResolvedSecretDecision
from core.secrets.models import SecretDisclosureEventRow, SecretHolderRow, SecretRow
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_CANARY = "the actual secret fact is that the vault code is nine nine four four"


def _phase() -> PhaseSpec:
    return PhaseSpec(
        label_key="test_phase",
        actors=[ActorSpec(persona_type="supervisor", mode="generate")],
        visibility=VisibilitySpec(
            knowledge_classes=[], scopes=["workspace_public"], entity_fields="all", secrets="none"
        ),
        budget=BudgetSpec(ratio={}, max_tokens=100),
    )


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, Principal]:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    async with tenant_scope(tenant_id) as session:
        viewer = Principal(tenant_id=tenant_id, kind="human", display_name="viewer")
        session.add(viewer)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=viewer.id,
                role="participant",
            )
        )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    return tenant_id, workspace_id, sess.id, viewer


async def _seed_secret_row(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, authored_by: uuid.UUID
) -> uuid.UUID:
    """A bare `SecretRow`, inserted directly -- `secret_disclosure_event`/`secret_holder`
    FK to a real secret, so the reveal tests (unlike the conceal ones, which never write
    a row referencing `secret_id` at all) need one to exist. `core.secrets.authoring
    .create_secret`'s permission gate isn't this test's concern."""
    async with tenant_scope(tenant_id) as session:
        row = SecretRow(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            subject_kind="entity",
            subject_id=uuid.uuid4(),
            content_ciphertext=_CANARY,
            gist="there is a vault with a code",
            disclosure_state="undisclosed",
            scope_key="workspace_public",
            authored_by=authored_by,
            version=1,
        )
        session.add(row)
        await session.flush()
        return row.id


async def test_concealed_secret_plaintext_absent_from_provider_payload(
    db_available: None,
) -> None:
    tenant_id, workspace_id, session_id, viewer = await _setup("exclusion-conceal-payload")
    resolved = ResolvedSecretDecision(
        secret_id=uuid.uuid4(),
        action="conceal",
        decision_id=None,
        content=None,
        hint_text=None,
        behavioral_directive="grows quiet whenever the vault comes up",
    )

    manifest = await assemble(
        viewer,
        _phase(),
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="tell me about the vault",
        query_embedding=unit_vector(0),
        history_max_tokens=100,
        resolved_secret_decisions=[resolved],
    )

    assert _CANARY not in manifest.rendered_context
    assert _CANARY not in manifest.volatile_suffix
    assert _CANARY not in manifest.stable_prefix


async def test_conceal_injects_behavioral_directive_in_place_of_content(
    db_available: None,
) -> None:
    tenant_id, workspace_id, session_id, viewer = await _setup("exclusion-conceal-directive")
    directive = "grows quiet whenever the vault comes up"
    resolved = ResolvedSecretDecision(
        secret_id=uuid.uuid4(),
        action="conceal",
        decision_id=None,
        content=None,
        hint_text=None,
        behavioral_directive=directive,
    )

    manifest = await assemble(
        viewer,
        _phase(),
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="tell me about the vault",
        query_embedding=unit_vector(0),
        history_max_tokens=100,
        resolved_secret_decisions=[resolved],
    )

    assert directive in manifest.rendered_context
    assert any(r.reason == "concealed" for r in manifest.redactions)

    # Removing the directive (the empty-directive lint scenario) leaves neither fact
    # nor motivation -- a content bug, not this module's job to paper over.
    empty_directive_resolved = ResolvedSecretDecision(
        secret_id=resolved.secret_id,
        action="conceal",
        decision_id=None,
        content=None,
        hint_text=None,
        behavioral_directive=None,
    )
    empty_manifest = await assemble(
        viewer,
        _phase(),
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="tell me about the vault",
        query_embedding=unit_vector(0),
        history_max_tokens=100,
        resolved_secret_decisions=[empty_directive_resolved],
    )
    assert directive not in empty_manifest.rendered_context
    assert _CANARY not in empty_manifest.rendered_context


async def test_reveal_is_atomic_across_context_event_and_holder_update(
    db_available: None,
) -> None:
    tenant_id, workspace_id, session_id, viewer = await _setup("exclusion-reveal")
    secret_id = await _seed_secret_row(tenant_id, workspace_id, viewer.id)
    real_holder_id = viewer.id
    nonexistent_holder_id = uuid.uuid4()  # violates secret_holder's principal FK

    resolved = ResolvedSecretDecision(
        secret_id=secret_id,
        action="reveal_full",
        decision_id=None,
        content=_CANARY,
        hint_text=None,
        behavioral_directive=None,
        disclosed_to_principal_ids=(real_holder_id, nonexistent_holder_id),
    )

    with pytest.raises(DBAPIError):
        await assemble(
            viewer,
            _phase(),
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            session_id=session_id,
            query_text="what is the vault code",
            query_embedding=unit_vector(0),
            history_max_tokens=100,
            resolved_secret_decisions=[resolved],
            disclosing_principal_id=viewer.id,
        )

    # A forced failure partway (the bogus holder) rolled back the whole transaction --
    # neither the event nor the legitimate holder exists.
    async with tenant_scope(tenant_id) as session:
        event = await session.scalar(
            select(SecretDisclosureEventRow).where(SecretDisclosureEventRow.secret_id == secret_id)
        )
        assert event is None
        holder = await session.scalar(
            select(SecretHolderRow).where(
                SecretHolderRow.secret_id == secret_id,
                SecretHolderRow.holder_principal_id == real_holder_id,
            )
        )
        assert holder is None


async def test_reveal_commits_event_and_holder_together_on_success(db_available: None) -> None:
    tenant_id, workspace_id, session_id, viewer = await _setup("exclusion-reveal-ok")
    secret_id = await _seed_secret_row(tenant_id, workspace_id, viewer.id)

    resolved = ResolvedSecretDecision(
        secret_id=secret_id,
        action="reveal_full",
        decision_id=None,
        content=_CANARY,
        hint_text=None,
        behavioral_directive=None,
        disclosed_to_principal_ids=(viewer.id,),
    )

    manifest = await assemble(
        viewer,
        _phase(),
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="what is the vault code",
        query_embedding=unit_vector(0),
        history_max_tokens=100,
        resolved_secret_decisions=[resolved],
        disclosing_principal_id=viewer.id,
    )

    assert _CANARY in manifest.rendered_context
    assert not any(r.id == str(secret_id) for r in manifest.redactions)

    async with tenant_scope(tenant_id) as session:
        event = await session.scalar(
            select(SecretDisclosureEventRow).where(SecretDisclosureEventRow.secret_id == secret_id)
        )
        assert event is not None
        assert event.mode == "full"
        holder = await session.scalar(
            select(SecretHolderRow).where(
                SecretHolderRow.secret_id == secret_id,
                SecretHolderRow.holder_principal_id == viewer.id,
            )
        )
        assert holder is not None
        assert holder.holder_kind == "told"
