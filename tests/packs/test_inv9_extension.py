"""extends the matrix to make the v1.2 INV-9 wording ("every shipped pack")
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

from core.entities.repo import get_schema
from core.entities.tags import TAG_WIDGET_REGISTRY, widget_for
from core.packs.loader import load_pack
from core.process.authoring import get_definition
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.interpreter import (
    ActorRef,
    ActorTurnResult,
    InterpreterContext,
    evaluate_gates,
)
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


async def test_every_swdev_flow_can_run_without_a_human_at_the_keyboard(
    pack_tenant: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """None of the three swdev flows could complete a turn.

    `mode: free` does not mean "any actor may speak" -- it means the turn is typed by a
    human, and the interpreter raises HumanTurnPendingError and parks the session as
    awaiting_human. Every phase where an engineer was meant to act declared it, including
    the *initial* phase of both standup and triage, so those two stalled before producing a
    single message. A pack whose flows cannot take a turn is the thing INV-9's "every
    shipped pack" wording exists to catch, and no assertion here was looking at it.

    This replaces a test that pinned `implement`'s `await` as a delegation placeholder.
    Delegation is real now (G4.16 landed as the delegate endpoint and its worker jobs), so
    the placeholder was obsolete *and* was a second reason that phase parked. The
    suspend/resume machinery it exercised is its own, covered by `test_awaits.py`.
    """
    tenant_id, workspace_id, _principal_id = pack_tenant
    loaded = await load_pack(_PACK_DIR, tenant_id, workspace_id)

    assert loaded.process_definition_ids, "the swdev pack shipped no flows at all"

    for key, definition_id in sorted(loaded.process_definition_ids.items()):
        definition_row = await get_definition(tenant_id, definition_id)
        assert definition_row is not None
        definition = ProcessDefinitionDSL.model_validate(definition_row.definition)

        for phase_key, phase in definition.phases.items():
            where = f"{key}.{phase_key}"
            for actor in phase.actors:
                # `free` is only ever right when the selector cannot resolve to an agent.
                # The interpreter raises per *resolved* actor, so a mixed
                # `any_of: [participant_agent, human_participant]` still parks on the agent
                # -- listing a human alongside does not rescue it.
                may_select_an_agent = actor.persona_type is not None or any(
                    entry.endswith("_agent") for entry in (actor.any_of or [])
                )
                assert actor.mode != "free" or not may_select_an_agent, (
                    f"{where}: mode 'free' on a selector that can resolve to an agent, "
                    f"which parks the session on a human-typed turn"
                )
            # An await is only a stall when nothing in the product can satisfy it.
            # `human_input` is exactly that in a pack meant to run unattended: there is
            # no human at this keyboard, so the phase waits out its timeout. A
            # `delegated_work` await is the opposite -- it exists because the phase
            # dispatched work that takes minutes, and the worker satisfies it when the
            # last job belonging to the session finishes (worker.session_wake). Forbidding
            # both would forbid the flow from waiting for the very work it commissioned,
            # which is what it did before: review reviewed an untouched repository and
            # merge found an empty queue, both reporting success.
            if phase.await_field is not None:
                assert phase.await_field.type == "delegated_work", (
                    f"{where}: an {phase.await_field.type!r} await parks an unattended "
                    f"flow with nobody to satisfy it"
                )


async def test_every_swdev_phase_is_reachable_and_every_flow_ends(
    pack_tenant: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """Two transition faults that only a running flow could expose.

    `merge` was unreachable: both of `review`'s gates are `on:` events, and event gates are
    structurally unmatchable where the interpreter evaluates them -- it does so once a
    phase's actors are exhausted, never on an external event -- while `review` declared no
    `on_complete`. A flow named for merging always ended at review.

    standup looped the other way: `wrap_up.on_complete` pointed back at `status_round`, so
    a flow that could take a turn would go round until the interpreter's step guard stopped
    it. It looked harmless only because its first phase blocked.
    """
    tenant_id, workspace_id, _principal_id = pack_tenant
    loaded = await load_pack(_PACK_DIR, tenant_id, workspace_id)

    for key, definition_id in sorted(loaded.process_definition_ids.items()):
        definition_row = await get_definition(tenant_id, definition_id)
        assert definition_row is not None
        definition = ProcessDefinitionDSL.model_validate(definition_row.definition)

        # Walk the flow the way the interpreter does once each phase's actors are spent.
        seen: list[str] = []
        phase_key: str | None = definition.initial_phase
        while phase_key is not None:
            assert phase_key not in seen, (
                f"{key}: phase {phase_key!r} is re-entered by exhaustion alone "
                f"({' -> '.join(seen)}) -- the flow never ends"
            )
            seen.append(phase_key)
            phase_key = evaluate_gates(definition.phases[phase_key], {})

        unreachable = set(definition.phases) - set(seen)
        assert not unreachable, (
            f"{key}: {sorted(unreachable)} cannot be reached by running the flow; "
            f"exhaustion walks {' -> '.join(seen)}"
        )


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
