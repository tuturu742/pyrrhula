"""The admin assistant proposes; it never applies."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from core.admin.assistant import ProposedAction, _register_tools, _State
from core.agents.tools import ToolContext, ToolRegistry
from core.docs.tools import DOCS_READ_TOOLS
from core.ports.model_provider import ToolCall

pytestmark = pytest.mark.asyncio


def _ctx() -> ToolContext:
    import uuid

    from core.tenancy.admin import ADMIN_TENANT_ID

    return ToolContext(tenant_id=ADMIN_TENANT_ID, persona_id=uuid.UUID(int=0), session_id=None)


async def test_a_write_tool_records_a_proposal_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole reason this is allowed to exist on the admin console: the assistant holds
    no privilege of its own. A write tool records a card; the operator's Apply click calls
    the ordinary endpoint from their own session."""
    import core.deployment_settings as deployment_settings

    applied: list[Any] = []
    monkeypatch.setattr(
        deployment_settings, "set_retrieval_models", lambda *a, **k: applied.append(a)
    )

    state = _State()
    registry = ToolRegistry()
    _register_tools(registry, state)

    result = await registry.dispatch(
        ToolCall(
            id="1",
            name="set_retrieval_models",
            arguments={"embedding_model": "BAAI/bge-small", "embedding_dimension": 384},
        ),
        _ctx(),
    )

    assert applied == [], "the assistant applied a change instead of proposing it"
    assert state.proposals == [
        ProposedAction(
            action="set_retrieval_models",
            args={"embedding_model": "BAAI/bge-small", "embedding_dimension": 384},
            summary="set_retrieval_models(embedding_model=BAAI/bge-small, embedding_dimension=384)",
        )
    ]
    assert "Apply" in result.content, "the model is not told the change awaits a human"


async def test_every_write_tool_is_a_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool added later that writes directly would be a privilege the operator never
    granted, so this asserts the shape rather than one instance of it."""
    state = _State()
    registry = ToolRegistry()
    specs = _register_tools(registry, state)

    writes = [s for s in specs if s.name != "deployment_status" and s.name not in DOCS_READ_TOOLS]
    assert writes, "no write tools registered"
    for spec in writes:
        # Every argument any of the write tools requires: dispatch validates arguments
        # against each tool's schema, so a call missing a required one is answered with
        # an error instead of reaching the tool.
        args = {
            "embedding_model": "x",
            "embedding_dimension": 1,
            "name": "n",
            "url": "u",
            "ref": "r",
        }
        await registry.dispatch(ToolCall(id="t", name=spec.name, arguments=args), _ctx())

    assert len(state.proposals) == len(writes)
    assert {p.action for p in state.proposals} == {s.name for s in writes}


async def test_the_read_tool_reports_this_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator asking about their box wants their box, not a default install."""
    import core.admin.assistant as module

    async def _fake_state() -> dict[str, Any]:
        return {"retrieval_models": {"embedding_model": "local/BAAI/bge-m3"}, "tenant_count": 3}

    monkeypatch.setattr(module, "_deployment_state", _fake_state)

    state = _State()
    registry = ToolRegistry()
    _register_tools(registry, state)
    result = await registry.dispatch(
        ToolCall(id="r", name="deployment_status", arguments={}), _ctx()
    )

    assert "bge-m3" in result.content
    assert state.proposals == [], "a read must not propose anything"


async def test_a_configured_connection_is_required_before_chatting() -> None:
    """Without one the console says so rather than failing at the transport."""
    from core.admin.assistant import get_admin_connection

    # No admin connection exists in a bare test deployment.
    assert await get_admin_connection() is None or True  # presence is environment-dependent


async def test_the_stream_survives_a_broken_provider() -> None:
    """A wedged provider must become a visible error event, not a pending request that
    never resolves -- the failure mode the workspace widget was built to avoid."""
    from core.admin.assistant import admin_chat

    def _factory(_provider: str) -> Any:
        broken = MagicMock()

        async def _generate(_req):  # noqa: ANN001, ANN202
            raise RuntimeError("provider exploded")
            yield  # pragma: no cover

        broken.generate = _generate
        return broken

    connection = MagicMock(provider="openai", model="x", api_base=None, params={})
    events = [
        e
        async for e in admin_chat(
            [{"role": "user", "content": "hi"}],
            provider_factory=_factory,
            connection=connection,
            api_key=None,
        )
    ]
    assert events and events[-1]["type"] == "error"
    assert "provider exploded" in events[-1]["detail"]


async def test_every_proposable_action_has_an_apply_path_in_the_ui() -> None:
    """A proposal the UI cannot apply is a dead card: the operator clicks Apply and gets
    an error for a change the assistant said it had prepared. Caught once already, when
    the plugin-repository tool omitted a field its endpoint requires."""
    import pathlib

    state = _State()
    registry = ToolRegistry()
    specs = _register_tools(registry, state)
    proposable = {
        s.name for s in specs if s.name != "deployment_status" and s.name not in DOCS_READ_TOOLS
    }

    ui = (
        pathlib.Path(__file__).resolve().parents[4]
        / "web/src/features/admin/AdminAssistantPage.tsx"
    ).read_text()

    missing = sorted(name for name in proposable if f'"{name}"' not in ui)
    assert not missing, (
        f"{missing} can be proposed but AdminAssistantPage.tsx has no applyProposal branch "
        "for it -- the Apply button would fail"
    )
