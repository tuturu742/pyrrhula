"""Per-workspace MCP registry.

**Allowlist, not blocklist.** `enabled_tools` is the complete set of tools that exist as
far as this workspace is concerned. A tool the server offers and the workspace has not
listed is never discovered, never described to a model, and never callable. and
`docs/agent-guide.md` say plainly that user-authored lore reaches tool-calling agents;
a blocklist's failure mode under that threat model is a tool nobody thought to block, which
is precisely the tool an attacker looks for.

**Effectfulness is a floor, not a fact.** A server declares which of its tools are
effectful; a workspace may add to that set and can never subtract from it. A server that
under-declares is a server whose "read-only" tool books a flight, and the asymmetry of
being wrong in each direction is not close.

`credential_ref` points into a secret manager and never holds a key -- the same rule
`agent.credential_ref` follows, for the same reason: a credential in a row is a
credential in every backup, every export, and every support ticket.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.ports.mcp import McpServerRef, McpToolSpec
from core.tenancy.models import Base
from core.tenancy.scope import tenant_scope

# The platform's own in-process tooling is addressed with this scheme: randomizer resolution,
# git delegation. Nothing leaves the process, no third party is involved, and no operator
# approval is meaningful. Anything else -- http(s), ws, stdio -- is an external endpoint.
INTERNAL_MCP_SCHEME = "pyrrhula://"


def is_external_mcp_url(url: str) -> bool:
    """True when this url names a third party rather than the platform's own tooling."""
    return not url.startswith(INTERNAL_MCP_SCHEME)


class McpServerRow(Base):
    __tablename__ = "mcp_server"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled_tools: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    effectful_tools: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    require_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Per-session ceiling on calls to this server (NULL = unlimited). An external MCP
    # server cannot budget per session -- it is sent only the model's arguments, never a
    # trusted session id -- so the cap belongs here, where the session IS known.
    max_calls_per_session: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # How long to wait on this server, and how much of its answer to accept. Both are
    # properties of the SERVER -- a lookup answers instantly, an engine tool runs a build
    # -- so one deployment-wide number would mean tuning for the slowest and letting
    # everything else hang that long when it dies. NULL = the platform default.
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_result_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Transport-specific knobs for THIS server (a SearXNG instance's engine list, and
    # whatever the next transport needs). A bag rather than a column per knob, so one
    # transport's vocabulary never lands in the generic registration.
    options: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("workspace_id", "key", name="uq_mcp_server_workspace_key"),
        Index("ix_mcp_server_workspace", "workspace_id"),
    )

    def to_ref(self) -> McpServerRef:
        return McpServerRef(
            key=self.key,
            url=self.url,
            credential_ref=self.credential_ref,
            timeout_seconds=self.timeout_seconds,
            max_result_chars=self.max_result_chars,
            options=dict(self.options or {}),
        )


class TenantMcpCapabilityRow(Base):
    """An admin-managed MCP capability granted at TENANT level (plan M-B): provisioned
    onto every workspace of the tenant, merged AFTER the workflow pack's declared
    servers -- extra tools against a customer's internal systems without authoring a
    custom workflow pack. Same shape and rules as ``McpServerRow``; this is the durable
    grant, ``mcp_server`` rows are its per-workspace materialization."""

    __tablename__ = "tenant_mcp_capability"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled_tools: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    effectful_tools: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    require_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_tenant_mcp_capability_key"),)


class CredentialInRegistryError(ValueError):
    """Someone tried to store what looks like a secret in `credential_ref`. Refused at the
    door: `credential_ref` is a *pointer*, and a registry that accepted keys would make
    "never commit provider API keys" a thing people remember rather than a thing that holds."""


# Deliberately crude. This is a guardrail against the obvious mistake (pasting a key into
# the field marked "credential"), not a secret detector -- the declarative
# secret-pattern scan is the real one, and pretending this is that would be worse than
# having neither.
_KEY_PREFIXES = ("sk-", "sk_live", "ghp_", "github_pat_", "xoxb-", "AKIA", "AIza")


# Sentinel distinguishing "caller didn't say" from an explicit None/False: a workflow
# re-apply that never mentions credential_ref must not wipe a credential an operator
# attached to the row, nor flip require_confirmation back to its default.
class _Unset:
    """A class, not ``object()``, so ``isinstance`` narrows each parameter for mypy."""


_UNSET = _Unset()


async def register_server(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    key: str,
    url: str,
    *,
    enabled_tools: list[str],
    effectful_tools: list[str] | None = None,
    credential_ref: str | None | _Unset = _UNSET,
    require_confirmation: bool | _Unset = _UNSET,
    max_calls_per_session: int | None | _Unset = _UNSET,
    timeout_seconds: int | None | _Unset = _UNSET,
    max_result_chars: int | None | _Unset = _UNSET,
    options: dict[str, object] | None | _Unset = _UNSET,
) -> McpServerRow:
    """Upsert by `(workspace, key)`, so re-running a deployment's registry setup is
    idempotent rather than a source of duplicates. Omitted ``credential_ref`` /
    ``require_confirmation`` preserve an existing row's values (defaults apply only on
    first creation)."""
    if (
        not isinstance(credential_ref, _Unset)
        and isinstance(credential_ref, str)
        and credential_ref.startswith(_KEY_PREFIXES)
    ):
        raise CredentialInRegistryError(
            "credential_ref must point into a secret manager, not hold a credential; the "
            f"value supplied looks like a live key ({credential_ref[:6]}…)"
        )

    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(McpServerRow).where(
                McpServerRow.workspace_id == workspace_id, McpServerRow.key == key
            )
        )
        row = existing or McpServerRow(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            key=key,
            url=url,
            credential_ref=None,
            require_confirmation=True,
        )
        row.url = url
        if not isinstance(credential_ref, _Unset):
            row.credential_ref = credential_ref
        row.enabled_tools = list(enabled_tools)
        row.effectful_tools = list(effectful_tools or [])
        if not isinstance(require_confirmation, _Unset):
            row.require_confirmation = bool(require_confirmation)
        if not isinstance(max_calls_per_session, _Unset):
            row.max_calls_per_session = (
                None if max_calls_per_session is None else int(max_calls_per_session)
            )
        if not isinstance(timeout_seconds, _Unset):
            row.timeout_seconds = None if timeout_seconds is None else int(timeout_seconds)
        if not isinstance(max_result_chars, _Unset):
            row.max_result_chars = None if max_result_chars is None else int(max_result_chars)
        if not isinstance(options, _Unset):
            row.options = dict(options or {})
        if existing is None:
            session.add(row)
        await session.flush()
        session.expunge(row)
        return row


async def list_servers(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> list[McpServerRow]:
    async with tenant_scope(tenant_id) as session:
        rows = list(
            (
                await session.execute(
                    select(McpServerRow)
                    .where(
                        McpServerRow.workspace_id == workspace_id,
                        McpServerRow.enabled.is_(True),
                    )
                    .order_by(McpServerRow.key)
                )
            ).scalars()
        )
        for row in rows:
            session.expunge(row)
        return rows


async def get_server(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, key: str
) -> McpServerRow | None:
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(McpServerRow).where(
                McpServerRow.workspace_id == workspace_id, McpServerRow.key == key
            )
        )
        if row is not None:
            session.expunge(row)
        return row


# ── tenant-level capability grants (admin-managed) ───────────────────────────────────
async def list_tenant_capabilities(tenant_id: uuid.UUID) -> list[TenantMcpCapabilityRow]:
    async with tenant_scope(tenant_id) as session:
        rows = list(
            (
                await session.execute(
                    select(TenantMcpCapabilityRow)
                    .where(TenantMcpCapabilityRow.tenant_id == tenant_id)
                    .order_by(TenantMcpCapabilityRow.key)
                )
            ).scalars()
        )
        for row in rows:
            session.expunge(row)
        return rows


async def upsert_tenant_capability(
    tenant_id: uuid.UUID,
    key: str,
    url: str,
    *,
    enabled_tools: list[str],
    effectful_tools: list[str] | None = None,
    credential_ref: str | None = None,
    require_confirmation: bool = True,
) -> TenantMcpCapabilityRow:
    if credential_ref and credential_ref.startswith(_KEY_PREFIXES):
        raise CredentialInRegistryError(
            "credential_ref must point into a secret manager, not hold a credential; the "
            f"value supplied looks like a live key ({credential_ref[:6]}…)"
        )
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(TenantMcpCapabilityRow).where(
                TenantMcpCapabilityRow.tenant_id == tenant_id,
                TenantMcpCapabilityRow.key == key,
            )
        )
        row = existing or TenantMcpCapabilityRow(tenant_id=tenant_id, key=key, url=url)
        row.url = url
        row.credential_ref = credential_ref
        row.enabled_tools = list(enabled_tools)
        row.effectful_tools = list(effectful_tools or [])
        row.require_confirmation = require_confirmation
        row.enabled = True
        if existing is None:
            session.add(row)
        await session.flush()
        session.expunge(row)
        return row


async def delete_tenant_capability(tenant_id: uuid.UUID, key: str) -> bool:
    """Remove the grant AND its per-workspace materializations."""
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(TenantMcpCapabilityRow).where(
                TenantMcpCapabilityRow.tenant_id == tenant_id,
                TenantMcpCapabilityRow.key == key,
            )
        )
        if row is None:
            return False
        await session.delete(row)
        servers = (
            await session.execute(
                select(McpServerRow).where(
                    McpServerRow.tenant_id == tenant_id, McpServerRow.key == key
                )
            )
        ).scalars()
        for server in servers:
            await session.delete(server)
        return True


async def apply_tenant_capabilities(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> list[str]:
    """Materialize every enabled tenant grant onto one workspace's allowlist. Called
    after the workflow pack's own servers wherever those are applied -- grants win a
    key collision with pack servers (the operator's explicit word beats pack defaults)."""
    applied: list[str] = []
    for grant in await list_tenant_capabilities(tenant_id):
        if not grant.enabled:
            continue
        await register_server(
            tenant_id,
            workspace_id,
            grant.key,
            grant.url.replace("{tenant_id}", str(tenant_id)),
            enabled_tools=list(grant.enabled_tools),
            effectful_tools=list(grant.effectful_tools),
            credential_ref=grant.credential_ref,
            require_confirmation=grant.require_confirmation,
        )
        applied.append(grant.key)
    return applied


@dataclass(frozen=True)
class AllowedTool:
    """A tool that survived the allowlist, with effectfulness already resolved. Carrying
    the resolution here rather than re-deriving it at the call site means the dispatcher
    cannot forget to widen a server's under-declaration."""

    server_key: str
    spec: McpToolSpec
    effectful: bool


def apply_allowlist(row: McpServerRow, discovered: list[McpToolSpec]) -> list[AllowedTool]:
    """Intersects what the server offers with what the workspace listed, and takes the
    union of the two effectfulness claims. A tool absent from `enabled_tools` produces no
    entry at all -- not a disabled one, because a disabled entry is still an entry a future
    code path could re-enable by reading past the flag."""
    allowed = set(row.enabled_tools)
    marked = set(row.effectful_tools)
    return [
        AllowedTool(
            server_key=row.key,
            spec=spec,
            effectful=spec.effectful or spec.name in marked,
        )
        for spec in discovered
        if spec.name in allowed
    ]


# The web-search preset asks for: a registry entry a deployment can enable, not a
# special code path. "Web search is just an MCP server behind a workspace policy flag" is
# only true if it is registered the same way everything else is.
# The resolution preset: the tenant's registered deterministic tools (whatever the
# selected workflow's pack defines -- a die roller, a card draw) served through the
# same MCP surface. In-process transport; the url carries the tenant address via the
# {tenant_id} placeholder workflow manifests use (substituted at registration).
RESOLUTION_PRESET = {
    "key": "resolution",
    "url": "pyrrhula://resolution/{tenant_id}",
    # The allowlist intersects with what the transport lists; workflows declare the
    # concrete tool keys their pack registers (empty here = nothing enabled).
    "enabled_tools": [],
    "effectful_tools": [],
    "require_confirmation": False,
}

WEB_SEARCH_PRESET = {
    "key": "web_search",
    "url": "https://mcp.example.invalid/web-search",
    "enabled_tools": ["search"],
    "effectful_tools": [],
    "require_confirmation": False,
}


WEB_FETCH_PRESET = {
    "key": "web_fetch",
    "url": "https://mcp.example.invalid/web-fetch",
    "enabled_tools": ["fetch"],
    "effectful_tools": [],
    "require_confirmation": False,
}


class McpCallRecord(Base):
    """Append-only ledger of external MCP calls, one row per call.

    Two jobs. It is what ``max_calls_per_session`` counts -- the platform knows the
    session an external server cannot be told about -- and it is the only durable trace a
    non-effectful call leaves at all (effectful calls have ``action_record``; read-only
    ones previously vanished, which is also why tool use was invisible in a session's
    record)."""

    __tablename__ = "mcp_call_record"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    server_key: Mapped[str] = mapped_column(String(63), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    effectful: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_mcp_call_record_session_server", "session_id", "server_key"),)


async def count_session_calls(tenant_id: uuid.UUID, session_id: uuid.UUID, server_key: str) -> int:
    """Calls this session has already spent against one server. Refusals are not counted
    -- a refused call reached nobody, and counting it would let a capped-out session
    burn its own error messages."""
    async with tenant_scope(tenant_id) as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(McpCallRecord)
                .where(
                    McpCallRecord.session_id == session_id,
                    McpCallRecord.server_key == server_key,
                    McpCallRecord.outcome != "refused",
                )
            )
            or 0
        )


async def record_call(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    server_key: str,
    tool_name: str,
    event_seq: int,
    effectful: bool,
    outcome: str,
    detail: dict[str, object] | None = None,
) -> None:
    async with tenant_scope(tenant_id) as session:
        session.add(
            McpCallRecord(
                tenant_id=tenant_id,
                session_id=session_id,
                server_key=server_key,
                tool_name=tool_name,
                event_seq=event_seq,
                effectful=effectful,
                outcome=outcome,
                detail=dict(detail or {}),
            )
        )
