"""C1.3 acceptance criteria for manifest persistence + read access control, against a
live Postgres.
"""

from __future__ import annotations

import uuid

import pytest

from adapters.permission.role_permission import RolePermissionService
from core.agents.seed import seed_dev_agent
from core.assembler.context_assembler import ContextManifest, ManifestEntry, Redaction, assemble
from core.assembler.manifest import (
    ManifestAccessDeniedError,
    ManifestNotFoundError,
    get_manifest_for_message,
    write_context_manifest,
)
from core.knowledge.authoring import create_source
from core.knowledge.retrieval.tests.conftest import (
    attach_to_workspace,
    seed_chunk,
    unit_vector,
)
from core.process.dsl.schema import ActorSpec, BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.skeleton import create_session
from core.sessions.models import MessageRow
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


def _phase() -> PhaseSpec:
    return PhaseSpec(
        label_key="test_phase",
        actors=[ActorSpec(persona_type="supervisor", mode="generate")],
        visibility=VisibilitySpec(
            knowledge_classes=["rules"],
            scopes=["workspace_public"],
            entity_fields="all",
            secrets="none",
        ),
        budget=BudgetSpec(ratio={"rules": 1.0}, max_tokens=500),
    )


async def _setup(
    slug_prefix: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, Principal, Principal]:
    """Returns (tenant_id, workspace_id, session_id, participant_viewer, other_participant)."""
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    async with tenant_scope(tenant_id) as session:
        viewer = Principal(tenant_id=tenant_id, kind="human", display_name="viewer")
        other = Principal(tenant_id=tenant_id, kind="human", display_name="other")
        session.add_all([viewer, other])
        await session.flush()
        session.add_all(
            [
                WorkspaceMembership(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    principal_id=viewer.id,
                    role="participant",
                ),
                WorkspaceMembership(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    principal_id=other.id,
                    role="participant",
                ),
            ]
        )

    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    return tenant_id, workspace_id, sess.id, viewer, other


async def _write_manifest_and_message(
    tenant_id: uuid.UUID, session_id: uuid.UUID, event_seq: int, viewer_id: uuid.UUID
) -> uuid.UUID:
    """Writes a manifest, then a message referencing it (mirroring what the real
    integration will do once wired into the agent runtime -- see manifest.py's
    module docstring on why that wiring doesn't exist yet)."""
    manifest = ContextManifest(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        session_id=session_id,
        rendered_context="<knowledge>...</knowledge>",
        stable_prefix="",
        volatile_suffix="<knowledge>...</knowledge>",
        entries=(),
        redactions=(),
        resolution_ids=(),
        token_counts={"total": 0},
        content_hash="a" * 64,
    )
    manifest_row = await write_context_manifest(
        tenant_id, session_id, event_seq, viewer_id, "test_phase", manifest
    )

    async with tenant_scope(tenant_id) as session:
        message = MessageRow(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=event_seq,
            author_principal_id=viewer_id,
            role="assistant",
            content_md="hello",
            context_manifest_id=manifest_row.id,
        )
        session.add(message)
        await session.flush()
        return message.id


async def test_write_and_read_manifest_via_real_assemble_output(db_available: None) -> None:
    tenant_id, workspace_id, session_id, viewer, _other = await _setup("manifest-write")
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    await attach_to_workspace(tenant_id, workspace_id, source.id)
    await seed_chunk(
        tenant_id,
        source.id,
        entry_key="grappling",
        body_text="Roll 1d20 plus strength to grapple.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )

    manifest = await assemble(
        viewer,
        _phase(),
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="grapple",
        query_embedding=unit_vector(0),
        history_max_tokens=0,
    )

    row = await write_context_manifest(tenant_id, session_id, 0, viewer.id, "test_phase", manifest)

    assert row.rendered_hash == manifest.content_hash
    assert row.token_counts == manifest.token_counts
    assert len(row.entries) == 1
    assert row.entries[0]["entry_key"] == "grappling"
    assert row.viewer_principal_id == viewer.id


async def test_at_most_one_manifest_per_session_event_seq(db_available: None) -> None:
    tenant_id, _workspace_id, session_id, viewer, _other = await _setup("manifest-unique")
    manifest = ContextManifest(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        session_id=session_id,
        rendered_context="a",
        stable_prefix="",
        volatile_suffix="a",
        entries=(),
        redactions=(),
        resolution_ids=(),
        token_counts={},
        content_hash="a" * 64,
    )
    first = await write_context_manifest(
        tenant_id, session_id, 0, viewer.id, "test_phase", manifest
    )

    # Retry-safe by design: a resumed turn re-executing at the same event_seq REUSES the
    # existing append-only row (same id) rather than raising on the unique constraint.
    second = await write_context_manifest(
        tenant_id, session_id, 0, viewer.id, "test_phase", manifest
    )
    assert second.id == first.id


# ── access control (D1.4's read path) ────────────────────────────────────────────────


async def test_exact_viewer_can_read_their_own_manifest(db_available: None) -> None:
    tenant_id, workspace_id, session_id, viewer, _other = await _setup("manifest-self-read")
    message_id = await _write_manifest_and_message(tenant_id, session_id, 0, viewer.id)

    row = await get_manifest_for_message(
        tenant_id,
        workspace_id,
        message_id,
        viewer.id,
        permission_service=RolePermissionService(),
    )
    assert row.viewer_principal_id == viewer.id


async def test_other_participant_cannot_read_someone_elses_manifest(db_available: None) -> None:
    """The literal acceptance criterion: player A cannot read the facilitator's (or any
    other participant's) manifest."""
    tenant_id, workspace_id, session_id, viewer, other = await _setup("manifest-deny")
    message_id = await _write_manifest_and_message(tenant_id, session_id, 0, viewer.id)

    with pytest.raises(ManifestAccessDeniedError):
        await get_manifest_for_message(
            tenant_id,
            workspace_id,
            message_id,
            other.id,
            permission_service=RolePermissionService(),
        )


async def test_facilitator_can_read_any_manifest_via_read_any_manifest_permission(
    db_available: None,
) -> None:
    tenant_id, workspace_id, session_id, viewer, _other = await _setup("manifest-facilitator")
    message_id = await _write_manifest_and_message(tenant_id, session_id, 0, viewer.id)

    async with tenant_scope(tenant_id) as session:
        facilitator = Principal(tenant_id=tenant_id, kind="human", display_name="facilitator")
        session.add(facilitator)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=facilitator.id,
                role="facilitator",
            )
        )
        facilitator_id = facilitator.id

    row = await get_manifest_for_message(
        tenant_id,
        workspace_id,
        message_id,
        facilitator_id,
        permission_service=RolePermissionService(),
    )
    assert row.viewer_principal_id == viewer.id  # read someone ELSE's manifest, successfully


async def test_message_without_a_manifest_raises_not_found(db_available: None) -> None:
    tenant_id, workspace_id, session_id, viewer, _other = await _setup("manifest-missing")
    async with tenant_scope(tenant_id) as session:
        message = MessageRow(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=0,
            author_principal_id=viewer.id,
            role="assistant",
            content_md="no manifest here",
        )
        session.add(message)
        await session.flush()
        message_id = message.id

    with pytest.raises(ManifestNotFoundError):
        await get_manifest_for_message(
            tenant_id,
            workspace_id,
            message_id,
            viewer.id,
            permission_service=RolePermissionService(),
        )


async def test_redactions_and_entries_round_trip_through_json_storage(
    db_available: None,
) -> None:
    tenant_id, _workspace_id, session_id, viewer, _other = await _setup("manifest-jsonshape")
    entry = ManifestEntry(
        citation_id="k1",
        chunk_id=uuid.uuid4(),
        entry_id=uuid.uuid4(),
        entry_key="rule",
        source_id=uuid.uuid4(),
        version_id=None,
        class_="rules",
        bucket="rules",
        rank=1,
        score=0.9,
        why="dense+sparse",
        token_count=12,
    )
    redaction = Redaction(type="secret", id="s1", reason="undisclosed")
    manifest = ContextManifest(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        session_id=session_id,
        rendered_context="x",
        stable_prefix="",
        volatile_suffix="x",
        entries=(entry,),
        redactions=(redaction,),
        resolution_ids=(uuid.uuid4(),),
        token_counts={"rules": 12, "total": 12},
        content_hash="b" * 64,
    )

    row = await write_context_manifest(tenant_id, session_id, 0, viewer.id, "test_phase", manifest)

    assert row.entries[0]["citation_id"] == "k1"
    assert row.entries[0]["chunk_id"] == str(entry.chunk_id)
    assert row.entries[0]["version_id"] is None
    assert row.redactions[0]["type"] == "secret"
    assert row.resolution_ids == [manifest.resolution_ids[0]]
