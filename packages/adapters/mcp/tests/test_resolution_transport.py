"""Resolution MCP transport: pack-registered tools exposed generically, server-side
outcomes persisted as ResolutionRecords, state-machine application. Live Postgres."""

from __future__ import annotations

import pathlib
import uuid

import pytest
from sqlalchemy import select

from adapters.mcp.resolution_transport import ResolutionMcpTransport
from adapters.permission.role_permission import RolePermissionService
from core.agents.seed import seed_dev_agent
from core.packs.loader import load_pack
from core.ports.mcp import McpServerRef, McpTransportError
from core.process.skeleton import create_session
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

pytestmark = pytest.mark.asyncio

_RPG = pathlib.Path(__file__).resolve().parents[4] / ".plugins" / "default" / "rpg"


async def _setup() -> tuple[uuid.UUID, uuid.UUID, McpServerRef]:
    tenant_id, principal_id, _ = await seed_dev_tenant(slug=f"restrans-{uuid.uuid4().hex[:8]}")
    async with tenant_scope(tenant_id) as session:
        workspace_id = await session.scalar(
            select(Workspace.id).where(Workspace.tenant_id == tenant_id)
        )
    await load_pack(_RPG, tenant_id, workspace_id)
    ref = McpServerRef(key="resolution", url=f"pyrrhula://resolution/{tenant_id}")
    return tenant_id, workspace_id, ref


async def test_lists_the_packs_registered_tools(db_available: None) -> None:
    _, _, ref = await _setup()
    transport = ResolutionMcpTransport(permission_service=RolePermissionService())
    names = {t.name for t in await transport.list_tools(ref)}
    # One tool. The pack used to register a second for coin flips; a coin is a rule
    # system now, and the surface is smaller for it.
    assert names == {"randomizer"}
    assert all(not t.effectful for t in await transport.list_tools(ref))


async def test_a_selected_rule_system_writes_a_resolution_record(db_available: None) -> None:
    """The coin survives the tool that used to carry it: the call names `coin_flip`
    and the one randomizer resolves in it."""
    tenant_id, workspace_id, ref = await _setup()
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    session_row = await create_session(tenant_id, workspace_id, persona_id)
    transport = ResolutionMcpTransport(permission_service=RolePermissionService())

    result = await transport.call_tool(
        ref,
        "randomizer",
        {
            "expression": "1d2",
            "check_type": "call",
            "rule_system": "coin_flip",
            "reason": "who goes first",
            "_context": {
                "session_id": str(session_row.id),
                "workspace_id": str(workspace_id),
                "event_seq": 0,
                "principal_id": str(persona_id),
            },
        },
    )
    assert result.structured is not None
    assert result.structured["outcome"] in ("heads", "tails")
    assert result.structured["resolution_id"]
    from core.resolution.records import ResolutionRecordRow

    async with tenant_scope(tenant_id) as session:
        row = await session.get(ResolutionRecordRow, uuid.UUID(result.structured["resolution_id"]))
        assert row is not None and row.session_id == session_row.id


async def test_requires_session_context(db_available: None) -> None:
    _, _, ref = await _setup()
    transport = ResolutionMcpTransport(permission_service=RolePermissionService())
    with pytest.raises(McpTransportError, match="inside sessions"):
        await transport.call_tool(ref, "randomizer", {"reason": "x"})


async def test_bad_tenant_address_fails_loudly() -> None:
    transport = ResolutionMcpTransport(permission_service=RolePermissionService())
    with pytest.raises(McpTransportError, match="tenant address"):
        await transport.list_tools(McpServerRef(key="resolution", url="nope"))


async def test_an_unloaded_rule_system_is_refused_not_substituted(db_available: None) -> None:
    """Falling back to the tool's own system would write a plausible record for a roll
    nobody asked for -- the failure INV-7 exists to prevent."""
    tenant_id, workspace_id, ref = await _setup()
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    session_row = await create_session(tenant_id, workspace_id, persona_id)
    transport = ResolutionMcpTransport(permission_service=RolePermissionService())

    with pytest.raises(McpTransportError, match="is not loaded"):
        await transport.call_tool(
            ref,
            "randomizer",
            {
                "expression": "1d20",
                "check_type": "strength_check",
                "rule_system": "no_such_system",
                "_context": {
                    "session_id": str(session_row.id),
                    "workspace_id": str(workspace_id),
                    "event_seq": 0,
                    "principal_id": str(persona_id),
                },
            },
        )
