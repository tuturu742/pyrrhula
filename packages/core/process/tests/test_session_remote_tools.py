"""The remote-tools synthetic phase must be a VALID PhaseSpec (learned the hard way:
an invalid literal here crashes every turn in a workspace with a registered remote
server), and the remote/preset routing predicate must never surface preset keys."""

from core.process.session_remote_tools import _is_remote, _remote_phase


def test_remote_phase_is_a_valid_phase_spec() -> None:
    phase = _remote_phase(["run_tests", "generate_image"])
    assert phase.tools == ["run_tests", "generate_image"]
    assert phase.visibility.entity_fields == []
    assert phase.visibility.secrets == "none"


def test_is_remote_excludes_presets_and_non_http() -> None:
    assert _is_remote("engine", "http://godot-mcp:8090/mcp")
    assert _is_remote("assets", "https://comfy.internal/mcp")
    assert not _is_remote("web_search", "https://searx.internal")
    # Not cosmetic: this path gates on workspace registration alone, so anything it
    # surfaces reaches personas whose own web_search flag is off.
    assert not _is_remote("web_fetch", "https://fetch.local/")
    assert not _is_remote("resolution", "pyrrhula://resolution/x")
    assert not _is_remote("git", "http://mcp-git:8080")
    assert not _is_remote("git-myrepo", "http://mcp-git:8080")
    assert not _is_remote("engine", "pyrrhula://something")


async def test_a_remote_tool_call_is_written_to_the_transcript_as_it_happens(
    db_available: None,
) -> None:
    """Before this, a call to a registered MCP server left one trace in the transcript: a
    count on the message that followed. The request and the answer -- or the refusal --
    were in a ledger with no reader. Now each call is a durable `tool_call` event at the
    seq the handler reserves, published live through the turn's event hook."""
    import uuid
    from dataclasses import dataclass, field
    from typing import Any

    from sqlalchemy import select

    from core.agents.seed import seed_dev_agent
    from core.agents.tools import ToolContext
    from core.mcp.registry import register_server
    from core.ports.mcp import McpServerRef, McpToolResult, McpToolSpec
    from core.process.session_remote_tools import make_remote_tool_handler
    from core.process.skeleton import create_session
    from core.sessions.models import SessionEventRow
    from core.tenancy.scope import tenant_scope
    from core.tenancy.seed import seed_dev_tenant

    @dataclass
    class _Lab:
        calls: list[dict[str, Any]] = field(default_factory=list)

        async def list_tools(self, server: McpServerRef) -> list[McpToolSpec]:
            return [McpToolSpec(name="evidence_check", description="", effectful=False)]

        async def call_tool(
            self, server: McpServerRef, name: str, arguments: dict[str, Any]
        ) -> McpToolResult:
            self.calls.append(dict(arguments))
            return McpToolResult(content="Page 6/6 reads: eleven pieces are missing.")

    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"toolev-{uuid.uuid4().hex[:8]}")
    await register_server(
        tenant_id,
        workspace_id,
        "evidence",
        "http://lab.example.invalid:8765",
        enabled_tools=["evidence_check"],
        require_confirmation=False,
        max_calls_per_session=1,
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    published: list[tuple[int, str, dict[str, Any]]] = []

    async def on_event(seq: int, kind: str, payload: dict[str, Any]) -> None:
        published.append((seq, kind, payload))

    handler = make_remote_tool_handler(
        workspace_id=workspace_id,
        server_key="evidence",
        tool_name="evidence_check",
        all_tool_names=["evidence_check"],
        transport=_Lab(),
        on_event=on_event,
        author="Petra Lind",
    )
    ctx = ToolContext(
        tenant_id=tenant_id, persona_id=persona_id, session_id=sess.id, turn_event_seq=5
    )
    first = await handler({"request": "recover_speech_file"}, ctx)
    second = await handler({"request": "operator_records"}, ctx)
    assert "eleven pieces" in first.content
    assert "session_call_cap_reached" in second.content

    async with tenant_scope(tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(SessionEventRow)
                    .where(
                        SessionEventRow.session_id == sess.id, SessionEventRow.kind == "tool_call"
                    )
                    .order_by(SessionEventRow.event_seq)
                )
            )
            .scalars()
            .all()
        )
    assert [r.payload["outcome"] for r in rows] == ["completed", "refused"]
    assert rows[0].payload["arguments"] == {"request": "recover_speech_file"}
    assert rows[0].payload["result"] == "Page 6/6 reads: eleven pieces are missing."
    assert rows[0].payload["author"] == "Petra Lind"
    assert "allowed calls" in rows[1].payload["message"]
    # Reserved past the turn's own slot, never colliding with its message.
    assert all(r.event_seq > 5 for r in rows)
    assert [(seq, kind) for seq, kind, _ in published] == [(r.event_seq, "tool_call") for r in rows]
