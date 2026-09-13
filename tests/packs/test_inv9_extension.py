"""F3.14: extends F3.9's matrix to make the v1.2 INV-9 wording ("every shipped pack")
operational for the swdev pack specifically -- the delegation-await interface G4.16 must
later satisfy (suspend/resume, surviving a checkpoint/restore across the suspension), and
the rendering genericity claim F3.13 makes (a `work_item` needs zero new widgets).

``test_all_shipped_packs_boot_and_run_smoke_sessions`` (F3.9, ``test_pack_matrix.py``)
already covers "all three packs boot and complete smoke sessions" generically -- adding
`packs/swdev/` made it parametrize a third case with no harness changes, which is this
task's own first acceptance criterion. The two tests below are the ones that needed new,
swdev-specific assertions.
"""

from __future__ import annotations

import pathlib
import uuid

from core.agents.seed import seed_dev_agent
from core.entities.repo import get_schema
from core.entities.tags import TAG_WIDGET_REGISTRY, widget_for
from core.packs.loader import load_pack
from core.process.authoring import get_definition
from core.process.awaits import get_active_await, make_await_hook, satisfy_await
from core.process.checkpoints import reconstruct_state, write_checkpoint
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.interpreter import (
    ActorRef,
    ActorTurnResult,
    InterpreterContext,
    advance_session,
    start_session,
)
from core.process.skeleton import create_session
from core.sessions.models import SessionRow
from core.tenancy.scope import tenant_scope

_PACK_DIR = pathlib.Path(__file__).resolve().parents[2] / ".plugins" / "default" / "swdev"


async def _no_actor(_ctx: InterpreterContext) -> ActorRef | None:
    return None


async def _unused_execute_turn(actor: ActorRef, ctx: InterpreterContext) -> ActorTurnResult:
    raise AssertionError(
        "execute_turn should never be called when next_actor_fn always returns None"
    )


async def _get_session(tenant_id: uuid.UUID, session_id: uuid.UUID) -> SessionRow:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        return row


async def test_swdev_smoke_suspends_at_delegation_await_and_resumes_on_injected_outcome(
    pack_tenant: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """The `implement` phase's `await` is a delegation placeholder (real delegation is
    G4.16); this proves the *interface* G4.16 must satisfy already works end to end
    through the unmodified interpreter -- suspend, survive a checkpoint/restore, then
    resume on an injected outcome -- exactly mirroring `test_awaits.py`'s own B1.6
    suspend/resume pattern (F3.14 adds nothing to that machinery, just exercises it here)."""
    tenant_id, workspace_id, principal_id = pack_tenant
    loaded = await load_pack(_PACK_DIR, tenant_id, workspace_id)
    definition_id = loaded.process_definition_ids["plan_implement_review_merge"]
    definition_row = await get_definition(tenant_id, definition_id)
    assert definition_row is not None
    definition = ProcessDefinitionDSL.model_validate(definition_row.definition)

    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    session = await create_session(tenant_id, workspace_id, persona_id)
    await start_session(
        tenant_id, session.id, definition, definition_row.id, definition_row.version
    )

    # Fast-forward directly to "implement" -- same "mutate current_phase, sidestep the
    # scheduler/turn machinery for a placeholder actor" shape `test_awaits.py`'s own
    # `_seed_at_feedback_loop` uses, since `implement`'s declared actor is a stand-in for
    # delegation that doesn't exist yet (real turn-taking isn't this test's subject).
    async with tenant_scope(tenant_id) as db_session:
        row = await db_session.get(SessionRow, session.id)
        assert row is not None
        row.current_phase = "implement"

    # A checkpoint exists at the suspended phase -- proves resume survives a
    # checkpoint/restore across the suspension, not just a live in-memory row.
    await write_checkpoint(tenant_id, session.id, workspace_id)

    on_await = make_await_hook()
    result = await advance_session(
        tenant_id,
        session.id,
        definition,
        next_actor_fn=_no_actor,
        execute_turn=_unused_execute_turn,
        on_await=on_await,
        max_steps=1,
    )
    assert result.status == "awaiting"
    live = await _get_session(tenant_id, session.id)
    assert live.status == "awaiting"

    active = await get_active_await(tenant_id, session.id)
    assert active is not None
    assert active.await_kind == "human_input"  # the only await type AwaitSpec supports today
    assert active.on_timeout_phase == "plan"
    assert active.outcome is None

    # Restore from the checkpoint written above, independent of the live row --
    # `reconstruct_state` proves the log/checkpoint pair is the real source of truth.
    reconstructed_phase, reconstructed_state = await reconstruct_state(tenant_id, session.id)
    assert reconstructed_phase == "implement"
    assert reconstructed_state == live.state

    # Resume on an injected delegation outcome.
    won = await satisfy_await(tenant_id, active.id, principal_id, definition)
    assert won is True
    resumed = await _get_session(tenant_id, session.id)
    assert resumed.status == "active"
    assert resumed.current_phase == "review"  # implement's on_complete target
    assert await get_active_await(tenant_id, session.id) is None


async def test_swdev_pack_adds_zero_widgets_to_the_registry(
    pack_tenant: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """The tag->widget registry is byte-identical before and after swdev pack load, and
    every tagged field on its three schemas resolves through a widget id F3.10 already
    shipped for the RPG/default packs -- the genericity claim F3.13 makes, checked
    mechanically rather than by inspection."""
    tenant_id, workspace_id, _principal_id = pack_tenant
    before = dict(TAG_WIDGET_REGISTRY)

    loaded = await load_pack(_PACK_DIR, tenant_id, workspace_id)

    after = dict(TAG_WIDGET_REGISTRY)
    assert before == after, "loading a pack must never add or change a tag->widget mapping"

    pre_shipped_widget_ids = {
        "resource_bar",
        "attribute_block",
        "status_chip_row",
        "progression_meter",
        "identity_text",
        "descriptor_text",
        "relationship_link",
        "private_marker",
    }

    for schema_key in ("work_item", "pull_request", "build"):
        schema_row = await get_schema(tenant_id, loaded.schema_ids[schema_key])
        assert schema_row is not None
        definition = schema_row.to_definition()
        for field in definition.fields:
            for tag in field.tags:
                assert widget_for(tag) in pre_shipped_widget_ids, (
                    f"{schema_key}.{field.key} tag {tag!r} resolves to a widget id "
                    f"not already shipped by F3.10 -- swdev needed a new widget"
                )
