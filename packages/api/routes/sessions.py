"""Session/message endpoints + SSE stream. T0.8's walking skeleton
(``core.process.skeleton``) still powers a session created *without* a
``process_definition_id``; one created *with* one runs through the real interpreter +
tool-calling agent runtime (B1.8, ``core.process.live_session``) instead. Both paths
coexist -- see ``B1.8-live-session-wiring.md`` for why this is additive, not a
replacement.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from starlette.responses import StreamingResponse

from api.authz import require_permission, require_tenant_permission
from api.embedding_provider_factory import get_embedding_provider
from api.encryptor_factory import get_encryptor
from api.job_queue_factory import get_job_queue
from api.mcp_transport_factory import get_mcp_transport
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from api.model_provider_factory import get_model_provider
from api.moderation_provider_factory import moderation_provider_for
from api.permission_service_factory import get_permission_service
from api.reranker_factory import get_reranker
from api.streaming.pubsub import publish_chunk, publish_event
from api.streaming.sse import sse_stream
from core.agents.authoring import get_persona, resolve_connection_api_key
from core.agents.models import Agent, Persona
from core.agents.override import (
    OverrideDraft,
    OverrideLeakError,
    SpeakAsDeniedError,
    draft_override,
    post_override,
)
from core.entities.mutation import transition as entity_transition
from core.entities.storage import EntityRow, get_entity
from core.mcp.registry import list_servers, register_server
from core.process.authoring import get_definition
from core.process.awaits import satisfy_await
from core.process.checkpoints import fork_session, list_checkpoints
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.dsl.validator import validate_raw
from core.process.interpreter import start_session as interpreter_start_session
from core.process.interpreter import submit_human_turn
from core.process.live_session import (
    DirectedTurnError,
    run_directed_persona_turn,
    run_process_definition_session,
)
from core.process.skeleton import create_session, generate_agent_response, submit_user_message
from core.repos.service import (
    RepoNotFoundError,
    list_session_repos,
    set_session_repos,
    store_key,
)
from core.sessions.lifecycle import (
    archive_session,
    get_session,
    last_event_times,
    list_session_roster,
    list_sessions,
    pause_session,
    rename_session,
    reopen_session,
    resume_session,
    set_conductor_wrap_up,
    set_session_agenda,
    set_session_roster,
    set_session_turn_policy,
)
from core.sessions.models import SessionPersonaRow, SessionRow
from core.sessions.notes import post_note
from core.tenancy.context import RequestContext
from core.tenancy.scope import tenant_scope
from core.workflows.service import tenant_workflow_grants_repo_access

router = APIRouter(
    prefix="/sessions",
    tags=["sessions"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


# The SSE stream lives on its OWN router without the rate-limit counters: it is one
# long-lived GET per open transcript, the cheapest request in the system -- and counting
# it starves real browsers: a 429'd EventSource retries every few seconds forever,
# feeding the very limiter that rejected it (observed live: every transcript rendered
# empty once the budget tripped). Authentication is unchanged.
stream_router = APIRouter(
    prefix="/sessions", tags=["sessions"], dependencies=[Depends(get_request_context)]
)


class CreateSessionRequest(BaseModel):
    workspace_id: uuid.UUID
    # Legacy single-persona skeleton path. For a real discussion pick a roster instead (#4):
    # one supervisor + one or more participants, which pins the session's actors explicitly.
    persona_id: uuid.UUID | None = None
    supervisor_persona_id: uuid.UUID | None = None
    participant_persona_ids: list[uuid.UUID] = []
    # #5: free-form agenda the supervisor is prompted to steer the session along.
    agenda_md: str | None = None
    # Optional display name so the session list stays distinguishable.
    name: str | None = None
    # #7: 'auto' (autonomous scheduler) or 'directed' (a human conducts each discussion
    # turn). Defaults to autonomous; it is also toggleable live via PATCH /turn-policy.
    turn_policy: Literal["auto", "directed"] = "auto"
    # B1.8: omitted (the default) keeps the T0.8 walking-skeleton path exactly as it
    # was; supplying a real, immutable process_definition_id (a specific version --
    # ProcessDefinitionRow rows are never "latest", see core.process.authoring's own
    # docstring) pins the session to the real interpreter instead.
    process_definition_id: uuid.UUID | None = None
    # Repos this session may delegate coding work to (from the tenant registry). Requires
    # the tenant's workflow to grant repo access; each selected repo's git MCP server is
    # registered on the workspace allowlist at creation.
    repo_ids: list[uuid.UUID] = []


class SessionResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    persona_id: uuid.UUID
    current_phase: str
    status: str
    state: dict[str, object]
    process_definition_id: uuid.UUID | None = None
    agenda_md: str | None = None
    turn_policy: str = "auto"
    name: str | None = None
    created_at: datetime | None = None
    last_event_at: datetime | None = None
    # Derived traffic-light for the list view: 'running' (something is happening now),
    # 'waiting' (paused / awaiting / idle), 'stopped' (flow reached its terminal phase).
    activity: str = "waiting"


_RECENT = timedelta(minutes=2)


def _activity(sess: Any, last_event_at: datetime | None) -> str:
    now = datetime.now(UTC)

    def recent(ts: datetime | None) -> bool:
        return ts is not None and (now - ts) < _RECENT

    if sess.status == "completed":
        # A finished FLOW can still have delegated work landing notes (PRs, reviews,
        # merge order) -- fresh events pulse green, idle finished sessions show red.
        return "running" if recent(last_event_at) else "stopped"
    if sess.status in ("paused", "awaiting", "awaiting_human"):
        return "waiting"
    return "running" if (recent(sess.claimed_at) or recent(last_event_at)) else "waiting"


def _session_response(sess: Any, last_event_at: datetime | None = None) -> SessionResponse:
    return SessionResponse(
        id=sess.id,
        workspace_id=sess.workspace_id,
        persona_id=sess.persona_id,
        current_phase=sess.current_phase,
        status=sess.status,
        state=sess.state,
        process_definition_id=sess.process_definition_id,
        agenda_md=sess.agenda_md,
        turn_policy=sess.turn_policy,
        name=sess.name,
        created_at=sess.created_at,
        last_event_at=last_event_at,
        activity=_activity(sess, last_event_at),
    )


@router.post("", status_code=201)
async def create_session_endpoint(
    body: CreateSessionRequest,
    background_tasks: BackgroundTasks,
    ctx: RequestContext = Depends(get_request_context),
) -> SessionResponse:
    # Roster path (#4): exactly one supervisor persona + >=1 participant. The supervisor is
    # also the session's primary persona (it owns the initial framing turn).
    roster: list[uuid.UUID] | None = None
    if body.supervisor_persona_id is not None:
        supervisor = await get_persona(ctx.tenant_id, body.supervisor_persona_id)
        if supervisor is None:
            raise HTTPException(status_code=404, detail="no such supervisor persona")
        if supervisor.persona_type != "supervisor":
            raise HTTPException(status_code=422, detail="the host persona must be a supervisor")
        if not body.participant_persona_ids:
            raise HTTPException(status_code=422, detail="select at least one participant persona")
        for pid in body.participant_persona_ids:
            p = await get_persona(ctx.tenant_id, pid)
            if p is None:
                raise HTTPException(status_code=404, detail=f"no such participant persona {pid}")
            if p.persona_type != "participant":
                raise HTTPException(status_code=422, detail=f"persona {pid} is not a participant")
        primary_persona_id = body.supervisor_persona_id
        roster = body.participant_persona_ids
    elif body.persona_id is not None:
        primary_persona_id = body.persona_id
    else:
        raise HTTPException(
            status_code=422, detail="provide supervisor_persona_id (+ participants) or persona_id"
        )

    try:
        sess = await create_session(ctx.tenant_id, body.workspace_id, primary_persona_id)
    except ValueError as exc:
        # Deliberately the same 404 whether the persona/workspace doesn't exist at all or
        # belongs to a different tenant -- RLS already makes the two indistinguishable,
        # and the HTTP layer shouldn't leak that distinction either.
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if roster is not None:
        await set_session_roster(ctx.tenant_id, sess.id, primary_persona_id, roster)

    if body.agenda_md is not None:
        await set_session_agenda(ctx.tenant_id, sess.id, body.agenda_md)

    # #7: pin the initial flow mode before the first advance, so a session launched
    # 'directed' parks for the conductor at the discussion phase instead of auto-running it.
    if body.turn_policy != "auto":
        await set_session_turn_policy(ctx.tenant_id, sess.id, body.turn_policy)

    # #repos: pin the session's repo selection and register each repo's git server on the
    # workspace allowlist (idempotent) -- the allowlist stays the one egress control, and
    # delegation later refuses repos outside this selection.
    if body.repo_ids:
        if not await tenant_workflow_grants_repo_access(ctx.tenant_id):
            raise HTTPException(
                status_code=409,
                detail="the tenant's workflow does not grant repo access; "
                "select a workflow with repo access first",
            )
        try:
            selected = await set_session_repos(ctx.tenant_id, sess.id, body.repo_ids)
        except RepoNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        for repo in selected:
            await register_server(
                ctx.tenant_id,
                body.workspace_id,
                f"git-{repo.key}",
                store_key(ctx.tenant_id, repo.key),
                enabled_tools=["delegate_work_item", "get_branch", "get_pull_request"],
                effectful_tools=["delegate_work_item"],
                require_confirmation=False,
            )

    if body.name:
        sess = await rename_session(ctx.tenant_id, sess.id, body.name)
    elif body.agenda_md is not None or body.turn_policy != "auto":
        refreshed = await get_session(ctx.tenant_id, sess.id)
        assert refreshed is not None
        sess = refreshed

    if body.process_definition_id is not None:
        definition_row = await get_definition(ctx.tenant_id, body.process_definition_id)
        if definition_row is None:
            raise HTTPException(
                status_code=404, detail=f"no process definition {body.process_definition_id}"
            )
        dsl, issues = validate_raw(definition_row.definition)
        if dsl is None or issues:
            issue_details = [{"field_path": i.field_path, "message": i.message} for i in issues]
            raise HTTPException(status_code=422, detail={"issues": issue_details})
        await interpreter_start_session(
            ctx.tenant_id, sess.id, dsl, definition_row.id, definition_row.version
        )
        refreshed = await get_session(ctx.tenant_id, sess.id)
        assert refreshed is not None
        sess = refreshed
        # The initial phase might not need human input at all (e.g. an agent-only
        # narration phase) -- the interpreter, not a human message, decides who goes
        # first, so kick off the first advance right away rather than leaving the
        # session sitting idle until someone happens to POST a message.
        background_tasks.add_task(
            _run_process_definition_advance, ctx.tenant_id, sess.id, body.process_definition_id
        )

    return _session_response(sess)


@router.get("")
async def list_sessions_endpoint(
    workspace_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> list[SessionResponse]:
    """Newest-first sessions in a workspace, for the UI's session list. Archived sessions
    are excluded (see ``core.sessions.lifecycle.list_sessions``). Each row carries its
    latest event time and a derived ``activity`` traffic-light so the list can show what
    is live right now without loading transcripts."""
    rows = await list_sessions(ctx.tenant_id, workspace_id)
    latest = await last_event_times(ctx.tenant_id, [r.id for r in rows])
    return [_session_response(r, latest.get(r.id)) for r in rows]


@router.get("/{session_id}")
async def get_session_endpoint(
    session_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> SessionResponse:
    """D1.3: the session view's initial-load/refresh fetch -- the SSE stream (below)
    only ever *replays events*, it never hands a caller today's already-current snapshot
    if they connect having missed nothing, e.g. right after ``create_session_endpoint``."""
    sess = await get_session(ctx.tenant_id, session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id}")
    latest = await last_event_times(ctx.tenant_id, [sess.id])
    return _session_response(sess, latest.get(sess.id))


class RenameSessionRequest(BaseModel):
    name: str | None


@router.patch("/{session_id}")
async def rename_session_endpoint(
    session_id: uuid.UUID,
    body: RenameSessionRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> SessionResponse:
    """Set (or clear, with null) the session's display name."""
    try:
        sess = await rename_session(ctx.tenant_id, session_id, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _session_response(sess)


@router.delete("/{session_id}", status_code=204)
async def archive_session_endpoint(
    session_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> None:
    """Soft-delete (archive) a session -- it leaves the session list, but its transcript
    stays viewable by id and its append-only records are untouched."""
    await require_tenant_permission(ctx, "session:archive")
    try:
        await archive_session(ctx.tenant_id, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # Best-effort: tear down the session's exec environments (no-op when none exist or no
    # env backend is configured -- the worker's Null provider removes zero).
    await get_job_queue().enqueue(
        ctx.tenant_id,
        "teardown_session_envs",
        {"session_id": str(session_id), "tenant_id": str(ctx.tenant_id)},
    )


class SetAgendaRequest(BaseModel):
    agenda_md: str | None = None


@router.patch("/{session_id}/agenda")
async def set_agenda_endpoint(
    session_id: uuid.UUID,
    body: SetAgendaRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> SessionResponse:
    """#5: set/clear the free-form agenda the supervisor is steered by."""
    try:
        await set_session_agenda(ctx.tenant_id, session_id, body.agenda_md)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    sess = await get_session(ctx.tenant_id, session_id)
    assert sess is not None
    return _session_response(sess)


class SetTurnPolicyRequest(BaseModel):
    turn_policy: Literal["auto", "directed"]


@router.patch("/{session_id}/turn-policy")
async def set_turn_policy_endpoint(
    session_id: uuid.UUID,
    body: SetTurnPolicyRequest,
    background_tasks: BackgroundTasks,
    ctx: RequestContext = Depends(get_request_context),
) -> SessionResponse:
    """#7: flip a session between autonomous (`auto`) and human-conducted (`directed`) live.
    Switching *to* `auto` kicks a fresh advance so the scheduler resumes participant turns;
    switching to `directed` just marks intent -- the next scheduling decision parks for the
    conductor (any turn already in flight finishes first)."""
    sess = await get_session(ctx.tenant_id, session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id}")
    await require_permission(ctx, "session:conduct", "workspace", sess.workspace_id)
    try:
        sess = await set_session_turn_policy(ctx.tenant_id, session_id, body.turn_policy)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if body.turn_policy == "auto" and sess.process_definition_id is not None:
        background_tasks.add_task(
            _run_process_definition_advance,
            ctx.tenant_id,
            session_id,
            sess.process_definition_id,
        )
    return _session_response(sess)


class RosterPersonaResponse(BaseModel):
    persona_id: uuid.UUID
    name: str
    persona_type: str
    is_supervisor: bool


@router.get("/{session_id}/personas")
async def list_session_personas_endpoint(
    session_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> list[RosterPersonaResponse]:
    """#7: the session's pinned roster, for the conductor panel -- who the overseer may
    direct a turn from or answer as. Supervisor first, then participants by name."""
    roster = await list_session_roster(ctx.tenant_id, session_id)
    out: list[RosterPersonaResponse] = []
    async with tenant_scope(ctx.tenant_id) as session:
        for entry in roster:
            persona = await session.get(Persona, entry.persona_id)
            if persona is None:
                continue
            out.append(
                RosterPersonaResponse(
                    persona_id=persona.id,
                    name=persona.name,
                    persona_type=persona.persona_type,
                    is_supervisor=entry.is_supervisor,
                )
            )
    out.sort(key=lambda r: (not r.is_supervisor, r.name.lower()))
    return out


class DirectedTurnRequest(BaseModel):
    persona_id: uuid.UUID


async def _run_directed_turn(
    tenant_id: uuid.UUID, session_id: uuid.UUID, persona_id: uuid.UUID
) -> None:
    """#7 background task: one model turn for the overseer-chosen persona in a conducted
    session, streamed over SSE. Never advances the phase -- the session stays parked at the
    conductable discussion phase; the overseer paces it."""
    sess = await get_session(tenant_id, session_id)
    if sess is None or sess.process_definition_id is None:
        return
    definition_row = await get_definition(tenant_id, sess.process_definition_id)
    if definition_row is None:
        return
    dsl, issues = validate_raw(definition_row.definition)
    if dsl is None or issues:
        return

    async def on_chunk(text: str) -> None:
        await publish_chunk(session_id, text)

    async def on_event(event_seq: int, kind: str, payload: dict[str, Any]) -> None:
        await publish_event(session_id, event_seq, kind, payload)

    try:
        await run_directed_persona_turn(
            tenant_id,
            session_id,
            persona_id,
            dsl,
            model_provider_factory=get_model_provider,
            embedding_provider=get_embedding_provider(),
            reranker=get_reranker(),
            on_chunk=on_chunk,
            on_event=on_event,
            encryptor=get_encryptor(),
            permission_service=get_permission_service(),
            mcp_transport=get_mcp_transport(),
            moderation_provider=await moderation_provider_for(tenant_id),
        )
    except DirectedTurnError:
        # A roster/phase setup error is not a session fault -- the pre-flight check in the
        # endpoint already validated the common cases; anything left is benign to drop here
        # (the turn simply never posts), never a reason to pause the session.
        return


@router.post("/{session_id}/turns/generate", status_code=202)
async def directed_turn_endpoint(
    session_id: uuid.UUID,
    body: DirectedTurnRequest,
    background_tasks: BackgroundTasks,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, bool]:
    """#7 managed flow: the overseer directs one model turn for a chosen roster persona.
    Gated on `session:conduct` (distinct from `agent:speak_as`, which answering *as* a
    persona still requires). Runs in the background and streams over SSE, like message
    submission; the phase does not advance."""
    sess = await get_session(ctx.tenant_id, session_id)
    if sess is None or sess.process_definition_id is None:
        raise HTTPException(
            status_code=404, detail=f"no process-definition-backed session {session_id}"
        )
    await require_permission(ctx, "session:conduct", "workspace", sess.workspace_id)
    # Only in directed mode: in auto mode the scheduler's own advance loop is the single
    # writer of turn event_seqs, and a directed turn racing it could collide on a sequence.
    if sess.turn_policy != "directed":
        raise HTTPException(
            status_code=409,
            detail="session is autonomous; switch it to directed mode to conduct turns",
        )

    async with tenant_scope(ctx.tenant_id) as session:
        in_roster = await session.scalar(
            select(SessionPersonaRow.id).where(
                SessionPersonaRow.session_id == session_id,
                SessionPersonaRow.persona_id == body.persona_id,
            )
        )
    if in_roster is None:
        raise HTTPException(status_code=422, detail="persona is not in this session's roster")

    background_tasks.add_task(_run_directed_turn, ctx.tenant_id, session_id, body.persona_id)
    return {"accepted": True}


@router.post("/{session_id}/conduct/wrap-up", status_code=202)
async def conduct_wrap_up_endpoint(
    session_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, bool]:
    """#7 managed flow: the overseer ends a conducted discussion. Sets `conductor_wrap_up`
    and advances -- the discussion phase's first gate then jumps to synthesis, where the
    supervisor delivers the closing turn autonomously."""
    sess = await get_session(ctx.tenant_id, session_id)
    if sess is None or sess.process_definition_id is None:
        raise HTTPException(
            status_code=404, detail=f"no process-definition-backed session {session_id}"
        )
    await require_permission(ctx, "session:conduct", "workspace", sess.workspace_id)
    if sess.turn_policy != "directed":
        raise HTTPException(status_code=409, detail="session is autonomous; nothing to wrap up")
    await set_conductor_wrap_up(ctx.tenant_id, session_id)
    background_tasks.add_task(
        _run_process_definition_advance,
        ctx.tenant_id,
        session_id,
        sess.process_definition_id,
    )
    return {"accepted": True}


class ContinueSessionRequest(BaseModel):
    # How many more autonomous rounds to grant when re-opening a finished round-table flow.
    # Ignored by the parked/awaiting path (nothing to extend) and by definitions with no
    # round budget. 1..10 keeps a fat-fingered value from spinning up a marathon.
    rounds: int = 1


@router.post("/{session_id}/continue", status_code=202)
async def continue_session_endpoint(
    session_id: uuid.UUID,
    body: ContinueSessionRequest,
    background_tasks: BackgroundTasks,
    ctx: RequestContext = Depends(get_request_context),
) -> SessionResponse:
    """#7 continue: keep a stopped discussion going. Two cases, decided from the pinned
    definition's *current* phase:

    * **Terminal** (the phase has no gates, no ``on_complete`` and no ``await`` -- e.g. a
      round-table sitting at ``synthesis``): re-open it at the phase flagged ``conductable``
      (the same signal the conduct gate keys on), granting ``rounds`` more autonomous rounds.
    * **Parked / awaiting** (still has an outgoing transition): leave the phase alone; just
      re-kick the interpreter so an autonomous run resumes.

    In ``auto`` mode this kicks a fresh advance; in ``directed`` mode it only re-opens the
    phase (the overseer then conducts turns / wraps up as usual). Gated on ``session:conduct``
    like the other conductor actions."""
    sess = await get_session(ctx.tenant_id, session_id)
    if sess is None or sess.process_definition_id is None:
        raise HTTPException(
            status_code=404, detail=f"no process-definition-backed session {session_id}"
        )
    await require_permission(ctx, "session:conduct", "workspace", sess.workspace_id)
    if sess.status == "paused":
        raise HTTPException(
            status_code=409, detail="session is paused; resume it before continuing"
        )
    if not (1 <= body.rounds <= 10):
        raise HTTPException(status_code=422, detail="rounds must be between 1 and 10")

    definition_row = await get_definition(ctx.tenant_id, sess.process_definition_id)
    if definition_row is None:
        raise HTTPException(
            status_code=409, detail="session pins a process definition that no longer exists"
        )
    definition = ProcessDefinitionDSL.model_validate(definition_row.definition)
    current = definition.phases.get(sess.current_phase)
    if current is None:
        raise HTTPException(
            status_code=409,
            detail=f"session is in phase {sess.current_phase!r}, which its definition "
            "does not declare",
        )

    is_terminal = not current.gates and current.on_complete is None and current.await_field is None
    if is_terminal:
        conductable = next(
            (key for key, ph in definition.phases.items() if "conductable" in ph.flags),
            None,
        )
        if conductable is None:
            raise HTTPException(
                status_code=409,
                detail="this flow has no re-openable (conductable) phase to continue into",
            )
        sess = await reopen_session(
            ctx.tenant_id, session_id, target_phase=conductable, rounds=body.rounds
        )

    if sess.turn_policy == "auto" and sess.process_definition_id is not None:
        background_tasks.add_task(
            _run_process_definition_advance,
            ctx.tenant_id,
            session_id,
            sess.process_definition_id,
        )
    return _session_response(sess)


class SessionRepoResponse(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    runtime: str
    # Whether this repo produces something a preview can serve, so the session view can
    # offer "deploy it" only where deploying means anything.
    artifact_name: str | None = None


@router.get("/{session_id}/repos")
async def list_session_repos_endpoint(
    session_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> list[SessionRepoResponse]:
    """The repos this session selected at launch -- what delegation may target."""
    rows = await list_session_repos(ctx.tenant_id, session_id)
    return [
        SessionRepoResponse(
            id=r.id,
            key=r.key,
            name=r.name,
            runtime=r.runtime,
            artifact_name=r.artifact_name,
        )
        for r in rows
    ]


async def _resolve_session_repo_server(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    repo_id: uuid.UUID | None,
    fallback_server_key: str,
) -> tuple[str, str | None]:
    """Which git server a delegate/review call targets. A session with selected repos is
    bound to them: an explicit ``repo_id`` must be in the selection (409), a sole selection
    is the default, several demand a choice (422). Only a session with *no* selection falls
    back to the legacy fixed ``server_key`` (pre-registry workspaces)."""
    selected = await list_session_repos(tenant_id, session_id)
    if repo_id is not None:
        match = next((r for r in selected if r.id == repo_id), None)
        if match is None:
            raise HTTPException(
                status_code=409, detail="that repo is not in this session's selection"
            )
        return f"git-{match.key}", str(match.id)
    if len(selected) == 1:
        return f"git-{selected[0].key}", str(selected[0].id)
    if len(selected) > 1:
        raise HTTPException(
            status_code=422,
            detail="this session has several repos; specify repo_id",
        )
    return fallback_server_key, None


def _select_assignee(
    named: str, devs: list[Any], by_name: dict[str, Any], index: int
) -> tuple[Any | None, bool]:
    """Which dev builds this work item.

    The name the item carries wins when it matches a dev on the roster -- that is what
    lets a supervisor match work to a person (by seniority, by ownership, by whatever it
    can reason about) instead of items landing in persona-id order. An unrecognised name
    falls back to the round robin and is reported rather than failing the batch: a typo in
    a plan should not stall every other item in it.

    Returns (persona, the_name_did_not_resolve).
    """
    fallback = devs[index % len(devs)] if devs else None
    if not named:
        return fallback, False
    chosen = by_name.get(named.strip().lower())
    if chosen is not None:
        return chosen, False
    return fallback, True


class DelegateRequest(BaseModel):
    # The work-item entities (by id) the facilitator is delegating -- one branch/PR each,
    # worked in parallel. repo_id picks the target among the session's selected repos
    # (optional when exactly one is selected); server_key is the legacy fallback for
    # sessions with no repo selection.
    work_item_ids: list[uuid.UUID]
    repo_id: uuid.UUID | None = None
    server_key: str = "git"
    # When true (default), the session's facilitator persona reviews each landed PR and
    # drives the review->fix loop itself (bounded); false = a human reviews via /review.
    auto_review: bool = True


async def _git_server(tenant_id: uuid.UUID, workspace_id: uuid.UUID, server_key: str) -> Any:
    for row in await list_servers(tenant_id, workspace_id):
        if row.key == server_key and "delegate_work_item" in row.enabled_tools:
            return row
    return None


async def _session_supervisor_principal(
    tenant_id: uuid.UUID, session_id: uuid.UUID
) -> uuid.UUID | None:
    """The dispatching engineer for delegation: the session's supervisor persona (it holds the
    facilitator workspace role, hence entity:mutate for driving the work-item FSM)."""
    for entry in await list_session_roster(tenant_id, session_id):
        if entry.is_supervisor:
            async with tenant_scope(tenant_id) as session:
                persona = await session.get(Persona, entry.persona_id)
                return persona.principal_id if persona is not None else None
    return None


@router.post("/{session_id}/delegate", status_code=202)
async def delegate_endpoint(
    session_id: uuid.UUID,
    body: DelegateRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, Any]:
    """D15: the facilitator approves the plan and delegates each work item to a coding agent.
    One background job per item (worked in parallel), each producing a real branch + PR on the
    server-side git store. Gated on session:conduct."""
    sess = await get_session(ctx.tenant_id, session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id}")
    await require_permission(ctx, "session:conduct", "workspace", sess.workspace_id)
    if not body.work_item_ids:
        raise HTTPException(status_code=422, detail="no work items to delegate")
    server_key, resolved_repo_id = await _resolve_session_repo_server(
        ctx.tenant_id, session_id, body.repo_id, body.server_key
    )
    server = await _git_server(ctx.tenant_id, sess.workspace_id, server_key)
    if server is None:
        raise HTTPException(
            status_code=409,
            detail=f"workspace has no {server_key!r} MCP server offering delegate_work_item",
        )
    viewer_principal = await _session_supervisor_principal(ctx.tenant_id, session_id)
    if viewer_principal is None:
        raise HTTPException(status_code=409, detail="session has no supervisor to dispatch as")

    # Each work item is assigned to a participant persona (round-robin over the roster,
    # stable order) -- the dev whose name the PR-opened and fix notes carry. The work
    # itself still dispatches under the supervisor's authority (permissions unchanged);
    # assignment is attribution, the thing a transcript reader actually wants to see.
    roster = await list_session_roster(ctx.tenant_id, session_id)
    dev_entries = sorted(
        (e for e in roster if not e.is_supervisor), key=lambda e: str(e.persona_id)
    )
    assignments: list[tuple[uuid.UUID, uuid.UUID | None, str]] = []
    unresolved: list[str] = []
    async with tenant_scope(ctx.tenant_id) as session:
        # The devs on this roster, indexed by the names a plan would actually write.
        devs: list[Persona] = []
        by_name: dict[str, Persona] = {}
        for entry in dev_entries:
            persona = await session.get(Persona, entry.persona_id)
            if persona is None:
                continue
            devs.append(persona)
            by_name[persona.name.strip().lower()] = persona
            by_name[persona.key.strip().lower()] = persona

        for i, work_item_id in enumerate(body.work_item_ids):
            # A work item carries an `assignee` field (swdev's schema has always had one);
            # honouring it is what lets a supervisor match work to a specific dev -- by
            # seniority, by ownership, by anything it can reason about -- instead of the
            # round robin below handing items out in persona-id order. Falling back rather
            # than failing on an unknown name keeps a typo in a plan from stalling the
            # whole batch; the name that did not resolve is reported instead.
            entity = await session.get(EntityRow, work_item_id)
            named = str((entity.data or {}).get("assignee") or "").strip() if entity else ""
            chosen, missed = _select_assignee(named, devs, by_name, i)
            if missed:
                unresolved.append(named)
            assignments.append(
                (work_item_id, chosen.id if chosen else None, chosen.name if chosen else "")
            )
    if unresolved:
        structlog.get_logger().warning(
            "delegation.assignee_unresolved",
            session_id=str(session_id),
            names=sorted(set(unresolved)),
        )

    # Reserve one event_seq per item so their branches (pyr/<session8>-<seq>) are distinct.
    n = len(body.work_item_ids)
    async with tenant_scope(ctx.tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        base_seq = row.next_event_seq
        row.next_event_seq = base_seq + n

    queue = get_job_queue()
    jobs: list[str] = []
    for i, (work_item_id, assignee_id, _name) in enumerate(assignments):
        job_id = await queue.enqueue(
            ctx.tenant_id,
            "delegate_work_item",
            {
                "tenant_id": str(ctx.tenant_id),
                "workspace_id": str(sess.workspace_id),
                "session_id": str(session_id),
                "event_seq": base_seq + i,
                "work_item_id": str(work_item_id),
                "viewer_principal_id": str(viewer_principal),
                "server_key": server_key,
                "repo_id": resolved_repo_id,
                "auto_review": body.auto_review,
                "assignee_persona_id": str(assignee_id) if assignee_id else None,
            },
        )
        jobs.append(str(job_id))

    # A batch of several PRs also gets the facilitator's recommended merge order --
    # enqueued after the delegate jobs, so the single-loop worker runs it once every PR
    # in the batch exists.
    if n >= 2:
        from core.actions.delegation import branch_name

        await queue.enqueue(
            ctx.tenant_id,
            "merge_order",
            {
                "tenant_id": str(ctx.tenant_id),
                "workspace_id": str(sess.workspace_id),
                "session_id": str(session_id),
                "store": server.url,
                "branches": [branch_name(session_id, base_seq + i) for i in range(n)],
            },
        )

    # One announcement from the facilitator: who is building what.
    if any(name for _, _, name in assignments):
        lines = []
        for work_item_id, _aid, name in assignments:
            entity = await get_entity(ctx.tenant_id, work_item_id)
            title = entity.name if entity is not None else str(work_item_id)
            lines.append(f"- **{title}** → {name or 'unassigned'}")
        with contextlib.suppress(Exception):  # announcement only; jobs are already queued
            await post_note(
                ctx.tenant_id,
                session_id,
                viewer_principal,
                "📋 Delegating work:\n" + "\n".join(lines),
            )
    return {"accepted": True, "jobs": jobs}


class ReviewRequest(BaseModel):
    work_item_id: uuid.UUID
    branch: str
    comment: str = "Please address review feedback."
    repo_id: uuid.UUID | None = None
    server_key: str = "git"


@router.post("/{session_id}/review", status_code=202)
async def review_endpoint(
    session_id: uuid.UUID,
    body: ReviewRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, Any]:
    """D15 review->fix: the facilitator requests changes on a delegated PR; the coding agent
    then pushes a fix commit to the same branch. Gated on session:conduct."""
    sess = await get_session(ctx.tenant_id, session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id}")
    await require_permission(ctx, "session:conduct", "workspace", sess.workspace_id)
    server_key, resolved_repo_id = await _resolve_session_repo_server(
        ctx.tenant_id, session_id, body.repo_id, body.server_key
    )
    server = await _git_server(ctx.tenant_id, sess.workspace_id, server_key)
    if server is None:
        raise HTTPException(status_code=409, detail=f"no {server_key!r} MCP server")
    viewer_principal = await _session_supervisor_principal(ctx.tenant_id, session_id)
    if viewer_principal is None:
        raise HTTPException(status_code=409, detail="session has no supervisor to review as")

    # The facilitator's review: request changes on the work item (in_review -> changes_requested).
    try:
        await entity_transition(
            viewer_principal,
            ctx.tenant_id,
            sess.workspace_id,
            body.work_item_id,
            "lifecycle",
            "request_changes",
            idempotency_key=f"review:{session_id}:{body.work_item_id}",
            permission_service=get_permission_service(),
            cause="human",
        )
    except Exception as exc:  # noqa: BLE001 -- report; the fix commit can still proceed
        raise HTTPException(status_code=409, detail=f"could not request changes: {exc}") from exc

    job_id = await get_job_queue().enqueue(
        ctx.tenant_id,
        "rework_work_item",
        {
            "tenant_id": str(ctx.tenant_id),
            "workspace_id": str(sess.workspace_id),
            "session_id": str(session_id),
            "work_item_id": str(body.work_item_id),
            "branch": body.branch,
            "comment": body.comment,
            "server_key": server_key,
            "repo_id": resolved_repo_id,
            "repo": server.url,
            "viewer_principal_id": str(viewer_principal),
        },
    )
    return {"accepted": True, "job": str(job_id)}


@router.post("/{session_id}/pause")
async def pause_session_endpoint(
    session_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> SessionResponse:
    try:
        sess = await pause_session(ctx.tenant_id, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _session_response(sess)


@router.post("/{session_id}/resume")
async def resume_session_endpoint(
    session_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    ctx: RequestContext = Depends(get_request_context),
) -> SessionResponse:
    try:
        sess = await resume_session(ctx.tenant_id, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # Resuming a paused autonomous session must also restart the interpreter --
    # flipping the status alone left the session "active" with no advance task, stuck
    # until someone happened to post a message or toggle the turn policy. Same kick as
    # session creation and the policy endpoint.
    if sess.turn_policy == "auto" and sess.process_definition_id is not None:
        background_tasks.add_task(
            _run_process_definition_advance, ctx.tenant_id, session_id, sess.process_definition_id
        )
    return _session_response(sess)


class OverrideDraftRequest(BaseModel):
    persona_id: uuid.UUID
    content_md: str
    mode: Literal["verbatim", "voice"] = "verbatim"


class OverrideDraftResponse(BaseModel):
    persona_id: uuid.UUID
    mode: Literal["verbatim", "voice"]
    original_content_md: str
    proposed_content_md: str
    rewrite_applied: bool


class OverridePostRequest(BaseModel):
    persona_id: uuid.UUID
    original_content_md: str
    confirmed_content_md: str
    mode: Literal["verbatim", "voice"] = "verbatim"


class OverrideMessageResponse(BaseModel):
    message_id: uuid.UUID
    event_seq: int
    was_human_override: bool
    rewrite_applied: bool


@router.post("/{session_id}/override/draft")
async def draft_override_endpoint(
    session_id: uuid.UUID,
    body: OverrideDraftRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> OverrideDraftResponse:
    """G4.4 step one: produce the text, do not post it. Verbatim mode is a passthrough;
    voice mode calls the model once and meters it. Nothing is persisted either way, so a
    human who changes their mind leaves no trace."""
    sess = await get_session(ctx.tenant_id, session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id}")

    agent: Agent | None = None
    provider = None
    api_key: str | None = None
    if body.mode == "voice":
        async with tenant_scope(ctx.tenant_id) as session:
            persona = await session.get(Persona, body.persona_id)
            if persona is None:
                raise HTTPException(status_code=404, detail=f"no agent {body.persona_id}")
            agent = await session.get(Agent, persona.agent_id)
            if agent is None:
                raise HTTPException(status_code=409, detail="agent has no model profile")
            session.expunge(agent)
        provider = get_model_provider(agent.provider)
        api_key = await resolve_connection_api_key(
            ctx.tenant_id, agent.credential_ref, encryptor=get_encryptor()
        )

    try:
        draft = await draft_override(
            ctx.tenant_id,
            sess.workspace_id,
            body.persona_id,
            ctx.principal_id,
            body.content_md,
            mode=body.mode,
            permission_service=get_permission_service(),
            provider=provider,
            agent=agent,
            api_key=api_key,
        )
    except SpeakAsDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return OverrideDraftResponse(
        persona_id=draft.persona_id,
        mode=draft.mode,
        original_content_md=draft.original_content_md,
        proposed_content_md=draft.proposed_content_md,
        rewrite_applied=draft.rewrite_applied,
    )


@router.post("/{session_id}/override", status_code=201)
async def post_override_endpoint(
    session_id: uuid.UUID,
    body: OverridePostRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> OverrideMessageResponse:
    """G4.4 step two: the human confirms, and only then does anything get written. The
    draft is rebuilt from the request rather than held server-side -- a stored draft would
    be state to expire, and the permission check runs again here regardless, so nothing is
    gained by trusting a server-side copy over the client's."""
    sess = await get_session(ctx.tenant_id, session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id}")

    try:
        agent = await _agent_principal(ctx.tenant_id, body.persona_id, sess.workspace_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    draft = OverrideDraft(
        persona_id=body.persona_id,
        agent_principal_id=agent,
        mode=body.mode,
        original_content_md=body.original_content_md,
        proposed_content_md=body.confirmed_content_md,
    )
    try:
        message = await post_override(
            ctx.tenant_id,
            sess.workspace_id,
            session_id,
            draft,
            ctx.principal_id,
            confirmed_content_md=body.confirmed_content_md,
            permission_service=get_permission_service(),
        )
    except SpeakAsDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except OverrideLeakError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return OverrideMessageResponse(
        message_id=message.id,
        event_seq=message.event_seq,
        was_human_override=message.was_human_override,
        rewrite_applied=message.rewrite_applied,
    )


async def _agent_principal(
    tenant_id: uuid.UUID, persona_id: uuid.UUID, workspace_id: uuid.UUID
) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        agent = await session.get(Persona, persona_id)
        if agent is None or agent.workspace_id != workspace_id:
            raise ValueError(f"no agent {persona_id} in this session's workspace")
        return agent.principal_id


class RecapResponse(BaseModel):
    job_id: uuid.UUID
    from_event_seq: int
    to_event_seq: int
    max_tokens: int


@router.post("/{session_id}/recap", status_code=202)
async def request_session_recap(
    session_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> RecapResponse:
    """G4.3's "resume into context": a human arriving from a digest link (or simply
    returning after a long absence) asks for the elapsed history to be repopulated, and
    gets back a job to poll rather than a request that blocks on a map-reduce over a
    month of session log.

    The recap is built **for the calling principal** -- G4.1's summariser filters its fact
    frame through that principal's own visibility, so two people opening the same link get
    two different, each-correct recaps. That is why the principal comes from the request
    context and is not a parameter a caller could set to somebody else.

    This is the entry point G4.1's scope note named as G4.3's to build; the summariser
    itself, its budget, and its provenance recording were all complete before this route
    existed."""
    sess = await get_session(ctx.tenant_id, session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id}")
    if sess.process_definition_id is None:
        raise HTTPException(
            status_code=409,
            detail=f"session {session_id} runs no process definition; it has no phase to "
            "resolve a history budget from",
        )

    definition_row = await get_definition(ctx.tenant_id, sess.process_definition_id)
    if definition_row is None:
        raise HTTPException(
            status_code=409,
            detail=f"session {session_id} pins a process definition that no longer exists",
        )
    definition = ProcessDefinitionDSL.model_validate(definition_row.definition)
    phase = definition.phases.get(sess.current_phase)
    if phase is None:
        raise HTTPException(
            status_code=409,
            detail=f"session {session_id} is in phase {sess.current_phase!r}, which its pinned "
            "definition does not declare",
        )

    async with tenant_scope(ctx.tenant_id) as session:
        agent = await session.get(Persona, sess.persona_id)
        if agent is None:
            raise HTTPException(status_code=409, detail="session's agent no longer exists")
        agent_id = agent.agent_id

    to_event_seq = max(sess.next_event_seq - 1, 0)
    job_id = await get_job_queue().enqueue(
        ctx.tenant_id,
        "summarise_history",
        {
            "tenant_id": str(ctx.tenant_id),
            "workspace_id": str(sess.workspace_id),
            "session_id": str(session_id),
            "viewer_principal_id": str(ctx.principal_id),
            "agent_id": str(agent_id),
            "phase": phase.model_dump(mode="json", by_alias=True),
            "from_event_seq": 0,
            "to_event_seq": to_event_seq,
            "max_tokens": phase.history_slice_tokens(),
        },
    )
    return RecapResponse(
        job_id=job_id,
        from_event_seq=0,
        to_event_seq=to_event_seq,
        max_tokens=phase.history_slice_tokens(),
    )


class CheckpointResponse(BaseModel):
    id: uuid.UUID
    event_seq: int
    phase: str
    created_at: str


@router.get("/{session_id}/checkpoints")
async def list_checkpoints_endpoint(
    session_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> list[CheckpointResponse]:
    """D1.3's fork-from-checkpoint entry point's picker list (B1.4's checkpoints, plan
    §5.4) -- oldest first, one row per phase transition this session has made."""
    checkpoints = await list_checkpoints(ctx.tenant_id, session_id)
    return [
        CheckpointResponse(
            id=cp.id, event_seq=cp.event_seq, phase=cp.phase, created_at=cp.created_at.isoformat()
        )
        for cp in checkpoints
    ]


class ForkSessionRequest(BaseModel):
    checkpoint_id: uuid.UUID


@router.post("/{session_id}/fork", status_code=201)
async def fork_session_endpoint(
    session_id: uuid.UUID,  # noqa: ARG001 -- the checkpoint id alone identifies the parent (B1.4)
    body: ForkSessionRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> SessionResponse:
    try:
        forked = await fork_session(ctx.tenant_id, body.checkpoint_id, created_by=ctx.principal_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _session_response(forked)


class SubmitMessageRequest(BaseModel):
    content: str


async def _run_generation(tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
    """Driven to completion as a FastAPI background task: the generator's side effects
    (persistence, publishing) all happen in its body regardless of whether anyone
    consumes the yielded text, so a plain drain loop is sufficient here."""

    async def on_chunk(text: str) -> None:
        await publish_chunk(session_id, text)

    async def on_event(event_seq: int, kind: str, payload: dict[str, Any]) -> None:
        await publish_event(session_id, event_seq, kind, payload)

    async for _ in generate_agent_response(
        tenant_id,
        session_id,
        model_provider_factory=get_model_provider,
        on_chunk=on_chunk,
        on_event=on_event,
    ):
        pass


# How many times the background task will re-enter the interpreter after it stops on its
# own per-advance step guard. Bounds total autonomous work while letting a long session
# (a multi-round interrogation, a full RPG scene) run to its real end.
_MAX_ADVANCE_CONTINUATIONS = 20


async def _run_process_definition_advance(
    tenant_id: uuid.UUID, session_id: uuid.UUID, process_definition_id: uuid.UUID
) -> None:
    """B1.8's background task: drives the real interpreter (possibly several agent
    turns + phase transitions in one call) instead of skeleton's one-shot generation."""
    definition_row = await get_definition(tenant_id, process_definition_id)
    assert definition_row is not None  # start_session already pinned a real row
    dsl, issues = validate_raw(definition_row.definition)
    assert dsl is not None and not issues  # re-validated once already, at session start

    async def on_chunk(text: str) -> None:
        await publish_chunk(session_id, text)

    async def on_event(event_seq: int, kind: str, payload: dict[str, Any]) -> None:
        await publish_event(session_id, event_seq, kind, payload)

    # ``advance_session`` stops at its runaway-loop guard (max_steps) and reports
    # 'active', meaning "nothing is blocking, there is simply more to do". Calling it
    # once and returning abandons the session right there: it stays 'active' with no
    # interpreter running and no fault to show, indistinguishable from working, until a
    # human happens to poke it. Keep advancing while it says 'active' -- every other
    # status ('awaiting', 'awaiting_human', 'paused', 'terminal') is a real stop.
    for _ in range(_MAX_ADVANCE_CONTINUATIONS):
        result = await run_process_definition_session(
            tenant_id,
            session_id,
            dsl,
            model_provider_factory=get_model_provider,
            embedding_provider=get_embedding_provider(),
            reranker=get_reranker(),
            on_chunk=on_chunk,
            on_event=on_event,
            encryptor=get_encryptor(),
            permission_service=get_permission_service(),
            mcp_transport=get_mcp_transport(),
            moderation_provider=await moderation_provider_for(tenant_id),
        )
        if result.status != "active":
            return
        if result.steps_taken == 0:
            # Defensive: 'active' with no progress would spin. Treat it as a stop.
            return


@router.post("/{session_id}/messages", status_code=202)
async def submit_message_endpoint(
    session_id: uuid.UUID,
    body: SubmitMessageRequest,
    background_tasks: BackgroundTasks,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, bool]:
    sess = await get_session(ctx.tenant_id, session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id}")

    # S3 (G4.14): human-authored session messages pass the authoring scan; a policy
    # whose action is 'block' withholds the message with the reasons.
    from core.moderation.hooks import scan_authored

    authored = await scan_authored(
        ctx.tenant_id,
        body.content,
        context="message",
        target_type="message",
        target_id=None,
        actor_principal_id=ctx.principal_id,
        provider=await moderation_provider_for(ctx.tenant_id),
    )
    if not authored.allowed:
        raise HTTPException(
            status_code=422,
            detail=f"message rejected by moderation: {', '.join(authored.reasons) or 'blocked'}",
        )

    async def on_event(event_seq: int, kind: str, payload: dict[str, Any]) -> None:
        await publish_event(session_id, event_seq, kind, payload)

    if sess.process_definition_id is not None:
        await submit_human_turn(
            ctx.tenant_id, session_id, body.content, ctx.principal_id, on_event=on_event
        )
        background_tasks.add_task(
            _run_process_definition_advance, ctx.tenant_id, session_id, sess.process_definition_id
        )
        return {"accepted": True}

    try:
        await submit_user_message(
            ctx.tenant_id, session_id, ctx.principal_id, body.content, on_event=on_event
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    background_tasks.add_task(_run_generation, ctx.tenant_id, session_id)
    return {"accepted": True}


class SatisfyAwaitRequest(BaseModel):
    await_state_id: uuid.UUID


class PendingAwaitOut(BaseModel):
    id: uuid.UUID
    await_kind: str
    timeout_at: datetime
    created_at: datetime


@router.get("/{session_id}/awaits")
async def list_pending_awaits(
    session_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> list[PendingAwaitOut]:
    """Pending await_state rows: what the session is blocked on. Without this read the
    satisfy endpoint required an id nothing exposed."""
    from core.sessions.models import AwaitStateRow

    async with tenant_scope(ctx.tenant_id) as session:
        rows = (
            await session.execute(
                select(AwaitStateRow)
                .where(
                    AwaitStateRow.session_id == session_id,
                    AwaitStateRow.outcome.is_(None),
                )
                .order_by(AwaitStateRow.created_at)
            )
        ).scalars()
        return [
            PendingAwaitOut(
                id=row.id,
                await_kind=row.await_kind,
                timeout_at=row.timeout_at,
                created_at=row.created_at,
            )
            for row in rows
        ]


@router.post("/{session_id}/await/satisfy")
async def satisfy_await_endpoint(
    session_id: uuid.UUID,
    body: SatisfyAwaitRequest,
    background_tasks: BackgroundTasks,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, bool]:
    """B1.8: the HTTP entry point for resolving a real ``await_state`` row (B1.6) --
    e.g. the ``feedback_loop``-style phases in ``STANDARD_SESSION_FLOW``. Advancing
    further afterward needs the same interpreter-driven background task human-turn
    submission does."""
    sess = await get_session(ctx.tenant_id, session_id)
    if sess is None or sess.process_definition_id is None:
        raise HTTPException(
            status_code=404, detail=f"no process-definition-backed session {session_id}"
        )

    definition_row = await get_definition(ctx.tenant_id, sess.process_definition_id)
    assert definition_row is not None
    dsl, issues = validate_raw(definition_row.definition)
    assert dsl is not None and not issues

    async def on_event(event_seq: int, kind: str, payload: dict[str, Any]) -> None:
        await publish_event(session_id, event_seq, kind, payload)

    won = await satisfy_await(
        ctx.tenant_id, body.await_state_id, ctx.principal_id, dsl, on_event=on_event
    )
    if won:
        background_tasks.add_task(
            _run_process_definition_advance, ctx.tenant_id, session_id, sess.process_definition_id
        )
    return {"satisfied": won}


@stream_router.get("/{session_id}/stream")
async def stream_session_endpoint(
    session_id: uuid.UUID, request: Request, ctx: RequestContext = Depends(get_request_context)
) -> StreamingResponse:
    return StreamingResponse(
        sse_stream(ctx.tenant_id, session_id, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
