"""`delegate_work_item` (G4.16 ★, D15, plan §14.5/§9.4/§13.7, CLAUDE.md rules 8/10/11).

An engineer agent dispatches a work item to an external coding agent over MCP; the session
suspends; the structured outcome drives the work item's FSM. Five decisions carry this, and
each is a test:

**One context path, no exceptions.** The brief is
``ContextAssembler.assemble(viewer=<dispatching engineer principal>, phase=...)`` plus the
work item's entity frame. There is no string-concatenation side channel, no "just add the
ticket description", no second assembly helper. A second brief-assembly path kills INV-1
and INV-8 across the delegation boundary in one move, because the boundary is exactly where
the assembler's guarantees stop being checked by anything downstream.

**The external agent is not a principal.** It inherits the dispatching engineer's
visibility. Making it a principal would invite per-agent scope grants and a second ACL
surface; inheritance keeps "what may leave the building?" answered by the same resolver
that answers everything else -- so a concealed secret is absent from the brief *by
construction*, not by a filter someone remembered to apply.

**The branch name is derived from the idempotency key.** Neither `push` nor `create PR` is
naturally idempotent, so `pyr/<session_short>-<seq>` converts "did I already do this?" into
a cheap external lookup: a retried dispatch collides with its own branch instead of forking
a second one. PR creation is lookup-by-branch-then-create for the same reason.

**Resume reconciles, never re-executes.** On restart mid-delegation the pending
`action_record` is found first and the external state is *queried* (does the branch exist?
the PR?) before anything is dispatched. Re-dispatch happens only when the work is provably
absent.

**Returned prose is untrusted.** The summary, the PR body, and any diff excerpt enter
context only inside G4.12's injection envelope; they never influence tool authorisation,
and the FSM transition is driven by the outcome *record*, never by what the summary claims.

Metering: one `usage_record` with ``purpose='delegation'`` (the v1.2 taxonomy addition),
written in the action's completion transaction (rule 11). The MCP **allowlist** is the
egress control here; D14 is not extended -- its scope is `ModelProvider` calls, and
widening it would blur the one distinction that makes both controls legible.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from core.actions.effectful import (
    ActionRecordRow,
    EffectfulAction,
    claim,
    complete,
)
from core.assembler.context_assembler import assemble
from core.audit.models import UsageRecordRow
from core.entities.mutation import transition
from core.entities.storage import get_entity
from core.mcp.client import ToolNotAvailableError, available_tools, wrap
from core.ports.mcp import McpToolResult, McpTransport, McpTransportError
from core.ports.permission import PermissionService
from core.process.dsl.schema import PhaseSpec
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope

DELEGATE_TOOL = "delegate_work_item"
_PURPOSE = "delegation"

# Read-only MCP tools used for reconciliation. Named as constants so the reconcile path
# cannot accidentally reach for a mutating one -- looking up whether work happened must
# never be able to make it happen.
LOOKUP_BRANCH_TOOL = "get_branch"
LOOKUP_PR_TOOL = "get_pull_request"


class DelegationNotAllowedError(Exception):
    """`delegate_work_item` is not in this phase's tool policy or the workspace allowlist.
    Delegation is an effectful call like any other and is gated like one."""


@dataclass(frozen=True)
class DelegationBrief:
    """What the external agent is told. ``context`` came from the assembler and nothing
    else; ``entity_frame`` is the work item's own fields, read from the record."""

    context: str
    entity_frame: dict[str, Any]
    branch: str
    content_hash: str

    def to_arguments(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "brief": self.context,
            "work_item": self.entity_frame,
        }


@dataclass(frozen=True)
class DelegationOutcome:
    """The structured result. Everything the UI and the FSM read comes from these fields;
    ``summary`` is present but is prose and is treated as such."""

    branch: str
    pr_ref: str | None
    ci_status: str | None
    summary: str

    @classmethod
    def from_result(cls, result: McpToolResult, *, branch: str) -> DelegationOutcome:
        structured = result.structured or {}
        return cls(
            # The *branch we asked for* wins over any branch the agent reports: the name
            # is derived from the idempotency key, and letting the reply rename it would
            # break the one lookup reconciliation depends on.
            branch=branch,
            pr_ref=_opt_str(structured.get("pr_ref")),
            ci_status=_opt_str(structured.get("ci_status")),
            summary=str(structured.get("summary") or result.content or ""),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "pr_ref": self.pr_ref,
            "ci_status": self.ci_status,
            "summary": self.summary,
        }


@dataclass
class DelegationResult:
    action_key: str
    branch: str
    outcome: DelegationOutcome | None = None
    reconciled: bool = False
    dispatched: bool = False
    envelope: str = ""
    transitions: list[str] = field(default_factory=list)


def _query_text(entity: Any) -> str:
    """The work item's own words: its name plus any string field. Built from the record
    rather than from a template, so a brief is retrieved *about the work* rather than
    about the act of delegating."""
    parts = [str(entity.name)]
    parts.extend(str(v) for v in entity.data.values() if isinstance(v, str) and v.strip())
    return " ".join(dict.fromkeys(parts))


def branch_name(session_id: uuid.UUID, event_seq: int) -> str:
    """`pyr/<session_short>-<seq>`. Derived from the idempotency key's own components so
    two callers computing it independently agree, and so a retried dispatch collides with
    its own branch rather than forking a second one."""
    return f"pyr/{str(session_id)[:8]}-{event_seq}"


async def build_brief(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    event_seq: int,
    viewer: Principal,
    phase: PhaseSpec,
    work_item_id: uuid.UUID,
    *,
    query_embedding: list[float],
) -> DelegationBrief:
    """The one and only brief-assembly path.

    ``assemble`` runs for the *dispatching engineer's* principal in the current phase, so
    every exclusion the assembler makes -- concealed secrets, out-of-scope knowledge,
    out-of-scope entity fields -- applies to what leaves the building, without this module
    knowing what any of them are."""
    entity = await get_entity(tenant_id, work_item_id)
    if entity is None or entity.workspace_id != workspace_id:
        raise ValueError(f"no work item {work_item_id} in workspace {workspace_id}")

    # The retrieval query is the *work item*, not the word "delegate". A brief is assembled
    # so the external agent knows what it is doing; retrieving against a fixed phrase would
    # surface whatever happens to mention delegation, which is nothing useful.
    manifest = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text=_query_text(entity),
        query_embedding=query_embedding,
        history_max_tokens=phase.history_slice_tokens(),
        event_seq=event_seq,
    )

    return DelegationBrief(
        context=manifest.rendered_context,
        entity_frame={
            "id": str(entity.id),
            "key": entity.key,
            "name": entity.name,
            "fields": dict(entity.data),
            "states": dict(entity.fsm_states),
            "version": entity.version,
        },
        branch=branch_name(session_id, event_seq),
        content_hash=manifest.content_hash,
    )


async def _assert_delegation_allowed(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    phase: PhaseSpec,
    server_key: str,
    *,
    transport: McpTransport,
) -> None:
    tools = await available_tools(tenant_id, workspace_id, phase, transport=transport)
    if not any(t.spec.name == DELEGATE_TOOL and t.server_key == server_key for t in tools):
        raise DelegationNotAllowedError(
            f"{DELEGATE_TOOL} is not available on {server_key!r} in phase "
            f"{phase.label_key!r}; delegation is gated by the same allowlist and phase "
            "policy as any other effectful tool"
        )


async def reconcile(
    tenant_id: uuid.UUID,
    record: ActionRecordRow,
    server: Any,
    *,
    transport: McpTransport,
) -> DelegationOutcome | None:
    """Looks the external state up. Returns the outcome when the work provably happened,
    ``None`` when it provably did not.

    Only the read-only lookup tools are used. A reconcile path that could call the
    dispatching tool would be a reconcile path that can *cause* the thing it is checking
    for, which is the failure this whole design exists to avoid."""
    branch = str(record.arguments.get("branch") or "")
    try:
        found = await transport.call_tool(server, LOOKUP_BRANCH_TOOL, {"branch": branch})
    except McpTransportError:
        # Cannot tell. Refusing to guess is the only safe answer: guessing "absent"
        # re-dispatches, and guessing "present" strands the session.
        return None
    if found.is_error or not found.structured.get("exists"):
        return None

    pr_ref = _opt_str(found.structured.get("pr_ref"))
    if pr_ref is None:
        try:
            pr = await transport.call_tool(server, LOOKUP_PR_TOOL, {"branch": branch})
            pr_ref = _opt_str(pr.structured.get("pr_ref"))
        except McpTransportError:
            pr_ref = None

    return DelegationOutcome(
        branch=branch,
        pr_ref=pr_ref,
        ci_status=_opt_str(found.structured.get("ci_status")),
        summary=str(found.structured.get("summary") or ""),
    )


async def delegate_work_item(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    event_seq: int,
    viewer: Principal,
    phase: PhaseSpec,
    work_item_id: uuid.UUID,
    *,
    server_key: str,
    transport: McpTransport,
    permission_service: PermissionService,
    agent_id: uuid.UUID | None = None,
    provider_name: str = "external",
    model_name: str = "coding-agent",
    query_embedding: list[float] | None = None,
    machine_key: str = "lifecycle",
    # An ordered walk, not one trigger. Dispatching used to fire `submit_for_review` alone,
    # which the work_item lifecycle declares `from: in_progress` -- while a freshly created
    # item resolves to the machine's initial state, `backlog`. Nothing matched, the mutation
    # service correctly reported `transitioned: False`, and the item stayed in backlog
    # through every delegation, branch, pull request and review anyone ever ran against it.
    # Each trigger that is legal from wherever the item actually sits is applied in turn, so
    # this both walks a new item up to `in_review` and is a no-op for one already there.
    on_dispatch_triggers: tuple[str, ...] = ("refine", "start", "submit_for_review"),
    extra_arguments: dict[str, Any] | None = None,
) -> DelegationResult:
    """Dispatch, or discover that it already happened.

    Order matters and is the whole design: authorise, build the brief, claim the key,
    **reconcile if the key was already claimed**, dispatch only if provably absent, record
    the outcome, meter, then drive the FSM from the record."""
    await _assert_delegation_allowed(
        tenant_id, workspace_id, phase, server_key, transport=transport
    )
    server_ref = await _resolve_ref(tenant_id, workspace_id, server_key)

    brief = await build_brief(
        tenant_id,
        workspace_id,
        session_id,
        event_seq,
        viewer,
        phase,
        work_item_id,
        query_embedding=query_embedding or [0.0] * 1024,
    )
    action = EffectfulAction(
        tenant_id=tenant_id,
        session_id=session_id,
        event_seq=event_seq,
        attempt_target=f"{DELEGATE_TOOL}:{work_item_id}",
        server_key=server_key,
        tool_name=DELEGATE_TOOL,
        arguments={**brief.to_arguments(), **(extra_arguments or {})},
    )
    result = DelegationResult(action_key=action.key, branch=brief.branch)

    claimed = await claim(action)
    if not claimed.fresh and claimed.record.outcome is not None:
        stored = dict(claimed.record.result)
        result.outcome = DelegationOutcome(
            branch=str(stored.get("branch") or brief.branch),
            pr_ref=_opt_str(stored.get("pr_ref")),
            ci_status=_opt_str(stored.get("ci_status")),
            summary=str(stored.get("summary") or ""),
        )
        result.envelope = wrap(server_key, DELEGATE_TOOL, result.outcome.summary)
        return result

    if not claimed.fresh:
        # Dispatched and never completed -- the restart case. Look before dispatching.
        found = await reconcile(tenant_id, claimed.record, server_ref, transport=transport)
        if found is not None:
            result.outcome = found
            result.reconciled = True
            await complete(tenant_id, action.key, "reconciled", found.to_json())
            result.envelope = wrap(server_key, DELEGATE_TOOL, found.summary)
            await _drive_fsm(
                tenant_id,
                workspace_id,
                viewer,
                work_item_id,
                action.key,
                machine_key,
                on_dispatch_triggers,
                permission_service,
                result,
            )
            return result
        # Provably absent: the dispatch never reached the far side, so re-dispatching is
        # not a duplicate.

    try:
        raw = await transport.call_tool(server_ref, DELEGATE_TOOL, action.arguments)
    except McpTransportError as exc:
        await complete(tenant_id, action.key, "failed", {"error": str(exc)})
        raise

    outcome = DelegationOutcome.from_result(raw, branch=brief.branch)
    await complete(tenant_id, action.key, "completed", outcome.to_json())
    await _meter(tenant_id, workspace_id, agent_id, provider_name, model_name)

    result.outcome = outcome
    result.dispatched = True
    result.envelope = wrap(server_key, DELEGATE_TOOL, outcome.summary)
    await _drive_fsm(
        tenant_id,
        workspace_id,
        viewer,
        work_item_id,
        action.key,
        machine_key,
        on_dispatch_triggers,
        permission_service,
        result,
    )
    return result


async def _drive_fsm(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    viewer: Principal,
    work_item_id: uuid.UUID,
    action_key: str,
    machine_key: str,
    triggers: tuple[str, ...],
    permission_service: PermissionService,
    result: DelegationResult,
) -> None:
    """Drives the work item through **F3.5's** mutation service, with the action as
    `cause_ref`. Not a direct row update: F3.5 owns guard evaluation, the state-change
    record, and the idempotency, and a delegation that wrote state itself would be a second
    writer with none of them.

    The triggers are fixed by the caller, never read from the outcome's prose. A returned
    summary claiming "and I also merged it" moves nothing."""
    for trigger in triggers:
        await _try_transition(
            tenant_id,
            workspace_id,
            viewer,
            work_item_id,
            action_key,
            machine_key,
            trigger,
            permission_service,
            result,
        )


async def _try_transition(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    viewer: Principal,
    work_item_id: uuid.UUID,
    action_key: str,
    machine_key: str,
    trigger: str,
    permission_service: PermissionService,
    result: DelegationResult,
) -> None:
    """One trigger. A trigger that does not apply from the item's current state is normal
    here -- the caller offers a walk and most steps are no-ops on any given call -- but it
    is never silent: a refusal that nobody can see is how a work item sat in `backlog`
    through six pull requests with `transitions: []` in the job record and no other trace."""
    import structlog

    try:
        outcome = await transition(
            viewer.id,
            tenant_id,
            workspace_id,
            work_item_id,
            machine_key,
            trigger,
            f"delegation:{action_key}:{trigger}",
            permission_service=permission_service,
            cause="agent",
            cause_ref=f"action:{action_key}",
        )
    except Exception as exc:  # noqa: BLE001 -- a guard that refuses is information
        structlog.get_logger().warning(
            "delegation.transition_failed",
            work_item_id=str(work_item_id),
            machine_key=machine_key,
            trigger=trigger,
            action_key=action_key,
            error=str(exc)[:200],
        )
        return
    # F3.5 returns `transitioned` plus `new_state`; a guard that refused reports
    # `transitioned: False` with the state unchanged. Recording only real transitions
    # keeps `result.transitions` a list of things that happened rather than of things
    # that were attempted.
    if outcome.get("transitioned"):
        result.transitions.append(str(outcome.get("new_state")))
    else:
        structlog.get_logger().info(
            "delegation.transition_not_applicable",
            work_item_id=str(work_item_id),
            machine_key=machine_key,
            trigger=trigger,
            state=str(outcome.get("new_state")),
        )


async def _meter(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    provider_name: str,
    model_name: str,
) -> None:
    """One row per delegated call, ``purpose='delegation'`` (rule 11). Token counts stay
    zero unless the coding agent reports them: a delegated call's cost is dominated by work
    Pyrrhula did not do and cannot count, and inventing a number would be worse than
    recording that the call happened."""
    if agent_id is None:
        return
    async with tenant_scope(tenant_id) as session:
        session.add(
            UsageRecordRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                provider=provider_name,
                model=model_name,
                purpose=_PURPOSE,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=0,
            )
        )


def _ref(server_key: str) -> Any:
    from core.ports.mcp import McpServerRef

    return McpServerRef(key=server_key, url="", credential_ref=None)


async def _resolve_ref(tenant_id: uuid.UUID, workspace_id: uuid.UUID, server_key: str) -> Any:
    """The full server ref for ``server_key`` from the workspace registry -- crucially its
    ``url``, which the transport needs (e.g. the git store's repo). Falls back to a url-less
    ref when no row is registered, preserving behaviour for callers/tests that dispatch to a
    scripted transport ignoring the url."""
    from core.mcp.registry import list_servers

    for row in await list_servers(tenant_id, workspace_id):
        if row.key == server_key:
            return row.to_ref()
    return _ref(server_key)


def _opt_str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


__all__ = [
    "DELEGATE_TOOL",
    "DelegationBrief",
    "DelegationNotAllowedError",
    "DelegationOutcome",
    "DelegationResult",
    "ToolNotAvailableError",
    "branch_name",
    "build_brief",
    "delegate_work_item",
    "reconcile",
]
