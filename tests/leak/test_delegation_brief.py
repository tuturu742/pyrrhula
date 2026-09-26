"""the leak criterion: a concealed secret held by the dispatching engineer is absent
from the delegation brief **by construction**.

Same canary method as E2.6 and G4.7, applied to the actual MCP dispatch payload -- the
bytes that would leave the building. The point is not that a filter ran; it is that the
brief comes from `assemble()`, whose exclusions apply without this module knowing what they
are, so there is nothing to forget to filter.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text

from adapters.encryptor.identity import IdentityEncryptor
from adapters.moderation.allow_all import AllowAllModerationProvider
from adapters.permission.role_permission import RolePermissionService
from core.actions.delegation import DELEGATE_TOOL, build_brief, delegate_work_item
from core.assembler.visibility import seed_default_scopes
from core.entities.repo import save_schema
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import create_entity
from core.knowledge.authoring import (
    EntryFields,
    attach_source_to_workspace,
    create_source,
    publish_version,
    upsert_draft_entry,
)
from core.mcp.registry import register_server
from core.ports.mcp import McpServerRef, McpToolResult, McpToolSpec
from core.process.dsl.schema import ActorSpec, BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.skeleton import create_session
from core.secrets.authoring import create_secret
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant
from tests.leak.test_export_modes import _embedding_hit, _exact_hit, _fuzzy_hit

_PERMISSIONS = RolePermissionService()
_SERVER = "coding-agent"

_CANARY = "the deploy key for the production cluster is kept in the second drawer"
_HIDDEN_LORE = "the release manager keeps a second drawer nobody has opened in years"


@dataclass
class _RecordingTransport:
    """Records the exact payload handed to the transport -- the bytes that would leave the
    building. Scanning the *dispatch arguments* rather than an intermediate object is what
    makes this a claim about what left, not about what a function returned."""

    dispatched: list[dict[str, Any]] = field(default_factory=list)

    async def list_tools(self, server: McpServerRef) -> list[McpToolSpec]:
        return [
            McpToolSpec(name=DELEGATE_TOOL, description="Delegate work", effectful=True),
            McpToolSpec(name="get_branch", description="Look up a branch", effectful=False),
        ]

    async def call_tool(
        self, server: McpServerRef, name: str, arguments: dict[str, Any]
    ) -> McpToolResult:
        self.dispatched.append({"tool": name, "arguments": arguments})
        return McpToolResult(
            content="Opened a pull request.",
            structured={"pr_ref": "PR-1", "ci_status": "passed", "summary": "Done."},
        )


def _phase() -> PhaseSpec:
    return PhaseSpec(
        label_key="implement",
        actors=[ActorSpec(persona_type="participant", mode="generate")],
        visibility=VisibilitySpec(
            knowledge_classes=["lore"],
            scopes=["workspace_public"],
            entity_fields="all",
            secrets="none",
        ),
        budget=BudgetSpec(ratio={"lore": 1.0}, max_tokens=4000),
        tools=[DELEGATE_TOOL],
    )


def scan(blob: str, secret: str) -> list[str]:
    return [
        name
        for name, fn in (
            ("exact", _exact_hit),
            ("fuzzy", _fuzzy_hit),
            ("embedding", _embedding_hit),
        )
        if fn(secret, blob)
    ]


async def test_delegation_brief_excludes_concealed_secrets_by_construction(
    db_available: None,
) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"deleg-leak-{uuid.uuid4().hex[:8]}"
    )
    await seed_default_scopes(tenant_id, workspace_id)

    async with tenant_scope(tenant_id) as session:
        engineer = Principal(tenant_id=tenant_id, kind="human", display_name="engineer")
        session.add(engineer)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=engineer.id,
                role="facilitator",
            )
        )
        await session.flush()
        session.expunge(engineer)

    # A secret the dispatching engineer *authored and holds* -- the case where an ad-hoc
    # brief-builder would most plausibly include it as "context the engineer already has".
    await create_secret(
        tenant_id,
        workspace_id,
        engineer.id,
        subject_kind="entity",
        subject_id=uuid.uuid4(),
        content=_CANARY,
        gist="a deploy key exists",
        scope_key="workspace_public",
        encryptor=IdentityEncryptor(),
        permission_service=_PERMISSIONS,
        moderation_provider=AllowAllModerationProvider(),
    )

    # Knowledge in a compartment the engineer cannot see, so the assembler has something to
    # exclude beyond the secret itself.
    source = await create_source(
        tenant_id, key=f"lore-{uuid.uuid4().hex[:8]}", name="Lore", class_="lore"
    )
    for entry_key, body, scope_key in (
        # Shares the work item's own words on purpose: `plainto_tsquery` ANDs its terms,
        # so a fixture whose lore does not contain them would retrieve nothing and the
        # exclusion assertions below would pass vacuously.
        ("public", "How to add the widget: the build is green first.", "workspace_public"),
        ("hidden", _HIDDEN_LORE, "facilitator_only"),
    ):
        await upsert_draft_entry(
            tenant_id,
            source.id,
            entry_key,
            EntryFields(title=entry_key, body_md=body, class_="lore", scope_key=scope_key),
        )
    version = await publish_version(tenant_id, source.id)
    await attach_source_to_workspace(
        tenant_id, workspace_id, source.id, "workspace_public", version_pin=version.id
    )
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text("UPDATE knowledge_entry SET constant = true WHERE knowledge_source_id = :s"),
            {"s": source.id},
        )
        await session.execute(
            text(
                "INSERT INTO knowledge_chunk "
                "(tenant_id, entry_id, version_id, ordinal, text, token_count, class, "
                " scope_key, embedding, content_hash) "
                "SELECT e.tenant_id, e.id, e.version_id, 0, e.body_md, 8, e.class, "
                "       e.scope_key, NULL, md5(e.body_md) "
                "FROM knowledge_entry e "
                "WHERE e.knowledge_source_id = :s AND e.version_id IS NOT NULL"
            ),
            {"s": source.id},
        )

    definition = EntitySchemaDefinition(fields=[FieldDef(key="title", type="string")])
    schema_row = await save_schema(tenant_id, workspace_id, "work_item", 1, definition)
    work_item = await create_entity(
        tenant_id,
        workspace_id,
        schema_row.id,
        definition,
        key=f"wi-{uuid.uuid4().hex[:8]}",
        name="Add the widget",
        scope_key="workspace_public",
        data={"title": "Add the widget"},
    )

    sess = await create_session(tenant_id, workspace_id, await _agent(tenant_id, workspace_id))
    await register_server(
        tenant_id,
        workspace_id,
        _SERVER,
        "https://mcp.example.invalid/coding-agent",
        enabled_tools=[DELEGATE_TOOL, "get_branch"],
        require_confirmation=False,
    )

    # The brief itself first.
    brief = await build_brief(
        tenant_id,
        workspace_id,
        sess.id,
        1,
        engineer,
        _phase(),
        work_item.id,
        query_embedding=[0.0] * 1024,
    )
    assert scan(brief.context, _CANARY) == [], (
        "the delegation brief carried a concealed secret's plaintext"
    )
    assert scan(str(brief.entity_frame), _CANARY) == []
    assert _HIDDEN_LORE not in brief.context
    assert "add the widget" in brief.context.lower(), (
        "the fixture proves nothing: the brief contained no knowledge at all, so excluding "
        "the hidden entry was not an exclusion doing work"
    )

    # Then the bytes actually handed to the transport.
    transport = _RecordingTransport()
    result = await delegate_work_item(
        tenant_id,
        workspace_id,
        sess.id,
        1,
        engineer,
        _phase(),
        work_item.id,
        server_key=_SERVER,
        transport=transport,
        permission_service=_PERMISSIONS,
    )
    assert result.dispatched is True
    assert transport.dispatched

    payload = str(transport.dispatched[0]["arguments"])
    hits = scan(payload, _CANARY)
    assert hits == [], (
        f"the MCP dispatch payload leaked a concealed secret: detectors {hits} fired. The "
        "brief comes from assemble(), whose exclusions apply without this module knowing "
        "what they are -- there is nothing here to forget to filter."
    )
    assert _HIDDEN_LORE not in payload


async def _agent(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> uuid.UUID:
    from core.agents.models import Persona
    from core.agents.seed import seed_dev_agent

    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    async with tenant_scope(tenant_id) as session:
        assert (
            await session.execute(select(Persona.id).where(Persona.id == persona_id))
        ).scalar_one()
    return persona_id
