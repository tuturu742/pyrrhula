"""The in-turn delegation tool: argument handling and the seam it closes.

The capability gap this covers cost three live runs. A pack's ``implement`` phase listed
``delegate_work_item`` in ``remote_tools``, but the git store is excluded from the remote
MCP path on purpose, so the tool was never registered and the supervisor correctly reported
it had no way to hand work over. These assert that the tool exists, that it is offered
exactly when a queue and the phase allow it, and that both callers dispatch through one
implementation.
"""

from __future__ import annotations

import inspect
import json
import uuid

import pytest

from core.agents.tools import ToolContext
from core.process.session_delegation_tools import (
    DELEGATE_TOOL_NAME,
    DELEGATE_TOOL_PARAMETERS,
    make_delegate_handler,
)

WORKSPACE = uuid.uuid4()


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def enqueue(self, tenant_id, kind, payload):  # noqa: ANN001, ANN201
        self.calls.append((kind, payload))
        return uuid.uuid4()


_NO_SESSION = object()


def _ctx(session_id: object = _NO_SESSION) -> ToolContext:
    return ToolContext(
        tenant_id=uuid.uuid4(),
        persona_id=uuid.uuid4(),
        session_id=uuid.uuid4() if session_id is _NO_SESSION else session_id,  # type: ignore[arg-type]
        turn_event_seq=0,
    )


@pytest.mark.asyncio
async def test_a_turn_without_a_session_cannot_delegate() -> None:
    handler = make_delegate_handler(workspace_id=WORKSPACE, queue=_RecordingQueue())
    result = await handler({"work_item_ids": [str(uuid.uuid4())]}, _ctx(session_id=None))
    assert json.loads(result.content)["error"] == "no_session"


@pytest.mark.asyncio
async def test_missing_or_empty_ids_are_refused_with_a_usable_message() -> None:
    handler = make_delegate_handler(workspace_id=WORKSPACE, queue=_RecordingQueue())
    for args in ({}, {"work_item_ids": []}, {"work_item_ids": "not-a-list-or-uuid"}):
        payload = json.loads((await handler(args, _ctx())).content)
        assert payload["error"] in {"missing_args", "invalid"}
        assert payload["message"]


@pytest.mark.asyncio
async def test_a_single_id_may_be_passed_bare() -> None:
    """Models routinely pass one id as a string rather than a one-element list. That is a
    JSON-shape quibble, not a reason to refuse work -- so it must reach dispatch, and fail
    (if it fails) for a real reason instead of 'work_item_ids must be a list'."""
    handler = make_delegate_handler(workspace_id=WORKSPACE, queue=_RecordingQueue())
    payload = json.loads((await handler({"work_item_ids": str(uuid.uuid4())}, _ctx())).content)
    assert payload.get("error") != "missing_args"


@pytest.mark.asyncio
async def test_a_non_uuid_id_names_itself_rather_than_stopping_at_invalid() -> None:
    handler = make_delegate_handler(workspace_id=WORKSPACE, queue=_RecordingQueue())
    payload = json.loads((await handler({"work_item_ids": ["the first one"]}, _ctx())).content)
    assert payload["error"] == "invalid"
    assert "the first one" in payload["message"]


def test_the_tool_declares_what_a_supervisor_needs_to_call_it() -> None:
    props = DELEGATE_TOOL_PARAMETERS["properties"]
    assert DELEGATE_TOOL_PARAMETERS["required"] == ["work_item_ids"]
    assert set(props) == {"work_item_ids", "auto_review", "repo_id"}


def test_the_tool_is_registered_when_a_queue_and_the_phase_allow_it() -> None:
    """Asserted on source: registration needs a live tenant, a persona and a manifest to
    exercise, and what regresses here is the condition, not the wiring."""
    from core.process import live_session

    # Whitespace-normalised, so the assertion is on the condition and not on how a
    # formatter happened to wrap it.
    src = " ".join(inspect.getsource(live_session).split())
    assert (
        "allowed_remote_tools is not None and DELEGATE_TOOL_NAME in allowed_remote_tools"
    ) in src, "a phase must ask for delegation by name, not get it by declaring nothing"
    assert "if job_queue is not None and wants_delegation:" in src


def test_a_phase_that_declares_no_remote_tools_gets_no_delegation() -> None:
    """ "No allowlist" means the phase declared none, not that it wants them all.

    Treating it as "allow everything" handed an effectful dispatch tool to an
    investigation phase whose job is to read a codebase and report. Its bench said so:
    "I have no tool that reads file contents -- the only function available to me is
    delegate_work_item", which was true and precisely backwards.
    """
    from core.process.dsl.schema import PhaseSpec

    bare = {
        "label_key": "phase.investigate",
        "actors": [],
        "visibility": {
            "knowledge_classes": [],
            "scopes": [],
            "entity_fields": "all",
            "secrets": "none",
        },
    }
    silent = PhaseSpec.model_validate(bare)
    asking = PhaseSpec.model_validate({**bare, "remote_tools": [DELEGATE_TOOL_NAME]})
    assert not silent.remote_tools
    assert asking.remote_tools == [DELEGATE_TOOL_NAME]


def test_both_callers_dispatch_through_one_implementation() -> None:
    """The HTTP endpoint used to own assignee selection, seq reservation, the merge-order
    job and the announcement. A second caller copying that would drift; it calls the same
    function instead."""
    from api.routes import sessions as routes

    src = inspect.getsource(routes.delegate_endpoint)
    assert "dispatch_work_items(" in src
    assert "queue.enqueue(" not in src
    assert routes._select_assignee.__module__ == "core.actions.dispatch"


def test_a_turn_renders_entity_state_so_ids_survive_the_phase_boundary() -> None:
    """The renderer was complete and unused: ``assemble()`` kept its no-op default
    because no caller ever passed the real one.

    The cost showed up as a delegation failure. A lead filed six work items in the planning
    phase, reached implement with a working ``delegate_work_item`` tool, and still could
    not call it -- the ids existed only in tool results, which the transcript does not
    replay. It said so and stopped rather than guessing uuids, which was the right call
    and the wrong outcome.
    """
    from core.process import live_session

    src = inspect.getsource(live_session)
    assert "entity_state_renderer=render_entity_state" in src
    assert "from core.entities.injection import render_entity_state" in src


def test_a_phase_can_declare_how_many_tool_calls_its_work_takes() -> None:
    """The runtime's cap guards against a model that never stops calling. It is the wrong
    number for a phase whose work genuinely takes more.

    Basic Fantasy character creation is six ability rolls and then the sheet: seven calls
    against a cap of eight. A single re-roll ended the turn with "tool loop exceeded 8
    iterations", which took the session with it -- and the phase had no way to say that
    seven was expected.
    """
    from core.process.dsl.schema import PhaseSpec

    bare = {
        "label_key": "phase.x",
        "actors": [],
        "visibility": {
            "knowledge_classes": [],
            "scopes": [],
            "entity_fields": "all",
            "secrets": "none",
        },
    }
    assert PhaseSpec.model_validate(bare).max_tool_calls is None
    assert PhaseSpec.model_validate({**bare, "max_tool_calls": 14}).max_tool_calls == 14


def test_the_declared_budget_reaches_the_turn() -> None:
    """A field nothing reads is the same as no field. The runtime default stays in force
    when a phase declares nothing."""
    from core.process import live_session

    src = inspect.getsource(live_session)
    assert 'max_tool_loop=getattr(phase, "max_tool_calls", None) or _DEFAULT_MAX_TOOL_LOOP' in src
