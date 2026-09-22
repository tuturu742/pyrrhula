"""Dispatching work items to coding agents -- the step between "the plan exists" and
"branches exist".

Delegation is never run inline: each work item becomes a ``delegate_work_item`` job that a
worker claims, because the work needs an exec environment and minutes, and the caller here
is either an HTTP request or a model turn. This module is the one place that decides *what*
gets enqueued -- target repo, assignee, branch-distinct event seqs, the merge-order job that
follows a batch -- so the HTTP endpoint and the in-turn tool cannot drift apart.

The caller supplies the ``JobQueue`` (rule 12): core states the intent, the composition root
supplies the port.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog

from core.agents.models import Persona
from core.entities.storage import EntityRow, get_entity
from core.mcp.registry import list_servers
from core.ports.job_queue import JobQueue
from core.repos.service import list_session_repos
from core.sessions.lifecycle import list_session_roster
from core.sessions.models import SessionRow
from core.sessions.notes import post_note
from core.tenancy.scope import tenant_scope

_log = structlog.get_logger()

DELEGATE_JOB = "delegate_work_item"


class DispatchError(Exception):
    """Dispatch cannot proceed, with a reason a caller can render verbatim. The HTTP layer
    maps ``status`` onto a response code; a model turn reads ``args[0]`` as the tool error."""

    def __init__(self, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class DispatchResult:
    job_ids: list[str]
    server_key: str
    repo_id: str | None
    # (work_item_id, assignee_persona_id, assignee_name) -- what the announcement renders.
    assignments: list[tuple[uuid.UUID, uuid.UUID | None, str]] = field(default_factory=list)
    unresolved_names: list[str] = field(default_factory=list)


async def resolve_session_repo_server(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    repo_id: uuid.UUID | None,
    fallback_server_key: str,
) -> tuple[str, str | None]:
    """Which git server a delegate/review call targets. A session with selected repos is
    bound to them: an explicit ``repo_id`` must be in the selection, a sole selection is the
    default, several demand a choice. Only a session with *no* selection falls back to the
    legacy fixed ``server_key`` (pre-registry workspaces)."""
    selected = await list_session_repos(tenant_id, session_id)
    if repo_id is not None:
        match = next((r for r in selected if r.id == repo_id), None)
        if match is None:
            raise DispatchError("that repo is not in this session's selection", status=409)
        return f"git-{match.key}", str(match.id)
    if len(selected) == 1:
        return f"git-{selected[0].key}", str(selected[0].id)
    if len(selected) > 1:
        raise DispatchError("this session has several repos; specify repo_id", status=422)
    return fallback_server_key, None


def select_assignee(
    named: str, devs: list[Any], by_name: dict[str, Any], index: int
) -> tuple[Any | None, bool]:
    """Which dev builds this work item.

    The name the item carries wins when it matches a dev on the roster -- that is what lets
    a supervisor match work to a person (by seniority, by ownership, by whatever it can
    reason about) instead of items landing in persona-id order. An unrecognised name falls
    back to the round robin and is reported rather than failing the batch: a typo in a plan
    should not stall every other item in it.

    Returns (persona, the_name_did_not_resolve).
    """
    fallback = devs[index % len(devs)] if devs else None
    if not named:
        # Nothing named: leave it undecided rather than positional. The worker asks the
        # session's facilitator, which can weigh the work against the roster -- and falls
        # back to this same round robin if that is unavailable. Choosing here would
        # pre-empt that with an ordering over persona ids.
        return None, False
    chosen = by_name.get(named.strip().lower())
    if chosen is not None:
        return chosen, False
    return fallback, True


async def git_server(tenant_id: uuid.UUID, workspace_id: uuid.UUID, server_key: str) -> Any:
    for row in await list_servers(tenant_id, workspace_id):
        if row.key == server_key and DELEGATE_JOB in row.enabled_tools:
            return row
    return None


async def session_supervisor_principal(
    tenant_id: uuid.UUID, session_id: uuid.UUID
) -> uuid.UUID | None:
    """The dispatching engineer: the session's supervisor persona (it holds the facilitator
    workspace role, hence entity:mutate for driving the work-item FSM)."""
    for entry in await list_session_roster(tenant_id, session_id):
        if entry.is_supervisor:
            async with tenant_scope(tenant_id) as session:
                persona = await session.get(Persona, entry.persona_id)
                return persona.principal_id if persona is not None else None
    return None


async def dispatch_work_items(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    work_item_ids: list[uuid.UUID],
    *,
    queue: JobQueue,
    repo_id: uuid.UUID | None = None,
    fallback_server_key: str = "git",
    auto_review: bool = True,
    announce: bool = True,
) -> DispatchResult:
    """Enqueue one coding-agent job per work item, plus the merge-order job a batch needs.

    Raises ``DispatchError`` for the conditions a caller must report rather than paper over:
    an empty batch, a repo the session did not select, a workspace with no git server, a
    session with no supervisor to dispatch as.
    """
    if not work_item_ids:
        raise DispatchError("no work items to delegate", status=422)

    server_key, resolved_repo_id = await resolve_session_repo_server(
        tenant_id, session_id, repo_id, fallback_server_key
    )
    server = await git_server(tenant_id, workspace_id, server_key)
    if server is None:
        raise DispatchError(
            f"workspace has no {server_key!r} MCP server offering {DELEGATE_JOB}", status=409
        )
    viewer_principal = await session_supervisor_principal(tenant_id, session_id)
    if viewer_principal is None:
        raise DispatchError("session has no supervisor to dispatch as", status=409)

    # Each work item is assigned to a participant persona (round-robin over the roster,
    # stable order) -- the dev whose name the PR-opened and fix notes carry. The work itself
    # still dispatches under the supervisor's authority (permissions unchanged); assignment
    # is attribution, the thing a transcript reader actually wants to see.
    roster = await list_session_roster(tenant_id, session_id)
    dev_entries = sorted(
        (e for e in roster if not e.is_supervisor), key=lambda e: str(e.persona_id)
    )
    assignments: list[tuple[uuid.UUID, uuid.UUID | None, str]] = []
    unresolved: list[str] = []
    async with tenant_scope(tenant_id) as session:
        devs: list[Persona] = []
        by_name: dict[str, Persona] = {}
        for entry in dev_entries:
            persona = await session.get(Persona, entry.persona_id)
            if persona is None:
                continue
            devs.append(persona)
            by_name[persona.name.strip().lower()] = persona
            by_name[persona.key.strip().lower()] = persona

        for i, work_item_id in enumerate(work_item_ids):
            entity = await session.get(EntityRow, work_item_id)
            named = str((entity.data or {}).get("assignee") or "").strip() if entity else ""
            chosen, missed = select_assignee(named, devs, by_name, i)
            if missed:
                unresolved.append(named)
            assignments.append(
                (work_item_id, chosen.id if chosen else None, chosen.name if chosen else "")
            )
    if unresolved:
        _log.warning(
            "delegation.assignee_unresolved",
            session_id=str(session_id),
            names=sorted(set(unresolved)),
        )

    # Reserve one event_seq per item so their branches (pyr/<session8>-<seq>) are distinct.
    n = len(work_item_ids)
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            raise DispatchError(f"no session {session_id} in this tenant", status=404)
        base_seq = row.next_event_seq
        row.next_event_seq = base_seq + n

    jobs: list[str] = []
    for i, (work_item_id, assignee_id, _name) in enumerate(assignments):
        job_id = await queue.enqueue(
            tenant_id,
            DELEGATE_JOB,
            {
                "tenant_id": str(tenant_id),
                "workspace_id": str(workspace_id),
                "session_id": str(session_id),
                "event_seq": base_seq + i,
                "work_item_id": str(work_item_id),
                "viewer_principal_id": str(viewer_principal),
                "server_key": server_key,
                "repo_id": resolved_repo_id,
                "auto_review": auto_review,
                "assignee_persona_id": str(assignee_id) if assignee_id else None,
            },
        )
        jobs.append(str(job_id))

    # A batch of several PRs also gets the facilitator's recommended merge order --
    # enqueued after the delegate jobs, so the single-loop worker runs it once every PR in
    # the batch exists.
    if n >= 2:
        from core.actions.delegation import branch_name

        await queue.enqueue(
            tenant_id,
            "merge_order",
            {
                "tenant_id": str(tenant_id),
                "workspace_id": str(workspace_id),
                "session_id": str(session_id),
                "store": server.url,
                "branches": [branch_name(session_id, base_seq + i) for i in range(n)],
            },
        )

    # One announcement from the facilitator: who is building what.
    if announce and any(name for _, _, name in assignments):
        lines = []
        for work_item_id, _aid, name in assignments:
            entity = await get_entity(tenant_id, work_item_id)
            title = entity.name if entity is not None else str(work_item_id)
            lines.append(f"- **{title}** → {name or 'unassigned'}")
        with contextlib.suppress(Exception):  # announcement only; jobs are already queued
            await post_note(
                tenant_id,
                session_id,
                viewer_principal,
                "📋 Delegating work:\n" + "\n".join(lines),
            )

    return DispatchResult(
        job_ids=jobs,
        server_key=server_key,
        repo_id=resolved_repo_id,
        assignments=assignments,
        unresolved_names=sorted(set(unresolved)),
    )
