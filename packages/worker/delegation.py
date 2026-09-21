"""Worker jobs for delegated coding work (D15).

``handle_delegate_work_item`` runs the full ``core.actions.delegation.delegate_work_item`` path
(authorise -> assemble brief -> claim -> reconcile-not-re-execute -> dispatch over the git MCP
transport -> record + meter -> drive the work-item FSM) for one work item. Enqueued one-per-item
by the facilitator's "approve & delegate" action, so items are worked in parallel.

``handle_rework_work_item`` is the review->fix step: it appends a commit to the *same* branch/PR
(the git transport reuses an existing branch) and drives the work item's ``rework`` transition.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

import structlog

from adapters.mcp.git_store import GitStore, GitStoreError, default_git_root
from core.actions.delegation import delegate_work_item
from core.entities.mutation import transition
from core.entities.storage import get_entity
from core.ports.mcp import McpServerRef
from core.process.dsl.schema import BudgetSpec, PhaseSpec, VisibilitySpec
from core.repos.service import RUNTIME_CATALOG, get_repo, store_key
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope
from worker.exec_env_factory import get_exec_env_provider
from worker.job_queue_factory import get_job_queue
from worker.mcp_transport_factory import get_mcp_transport
from worker.permission_service_factory import get_permission_service
from worker.review import max_review_rounds
from worker.transcript import post_note

log = structlog.get_logger()


async def _store_key_for(tenant_id: uuid.UUID, repo_id: str | None) -> str | None:
    if not repo_id:
        return None
    repo = await get_repo(tenant_id, uuid.UUID(repo_id))
    return store_key(tenant_id, repo.key) if repo is not None else None


async def _pr_summary_line(skey: str | None, branch: str) -> str:
    """'PR #1 (url) — 3 files changed, +120 −4, CI: passed' from the store's records.
    Best-effort: a note with fewer numbers beats a failed job."""
    if skey is None:
        return f"branch `{branch}`"
    store = GitStore(default_git_root())
    parts: list[str] = []
    try:
        pr = await store.get_pr(skey, branch) or {}
        ref = pr.get("pr_ref") or branch
        parts.append(f"**{ref}**" + (f" ({pr['html_url']})" if pr.get("html_url") else ""))
        if pr.get("ci_status"):
            parts.append(f"CI: **{pr['ci_status']}**")
    except GitStoreError:
        parts.append(f"branch `{branch}`")
    try:
        stat = await store.diff_stat(skey, branch)
        parts.insert(
            1,
            f"{stat['files']} file(s) changed, +{stat['insertions']} −{stat['deletions']}",
        )
    except GitStoreError:
        pass
    return " — ".join(parts)


def _remote_config(tenant_id: uuid.UUID, repo: Any) -> dict[str, Any] | None:
    """The push-back target: the registration's source_url + opaque credential_ref (the
    transport resolves the token itself; nothing secret is persisted in job payloads or
    action records)."""
    url = (repo.source_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    return {
        "url": url,
        "provider": repo.provider,
        "credential_ref": str(repo.credential_ref) if repo.credential_ref else None,
        "tenant_id": str(tenant_id),
    }


async def _environment_config(tenant_id: uuid.UUID, repo_id: str | None) -> dict[str, Any] | None:
    """The exec-environment config for a delegation, from the repo's registration: the
    curated runtime's image + baseline setup, the repo's own setup commands, and its test
    command. None (-> direct-store fallback) when no repo is bound to the call."""
    if not repo_id:
        return None
    repo = await get_repo(tenant_id, uuid.UUID(repo_id))
    if repo is None:
        return None
    runtime = RUNTIME_CATALOG.get(repo.runtime) or RUNTIME_CATALOG["debian"]
    # A custom image overrides the catalog entry (its baseline setup is meaningless for
    # an arbitrary image; the repo's own setup_cmds still run).
    if repo.runtime_image:
        image, baseline = repo.runtime_image, []
    else:
        raw_setup = runtime.get("setup")
        image = str(runtime["image"])
        baseline = [str(c) for c in raw_setup] if isinstance(raw_setup, list) else []
    from core.exec_engines import engine_by_key, get_tenant_engine_key

    engine_key = await get_tenant_engine_key(tenant_id)
    engine = engine_by_key(engine_key) or {}
    return {
        "image": image,
        "setup_cmds": [*baseline, *(repo.setup_cmds or [])],
        "test_cmd": repo.test_cmd,
        "build_cmd": repo.build_cmd,
        "artifact_name": repo.artifact_name,
        "engine": engine_key,
        # Per-engine route to the api's git smart-HTTP (remote engines' environments
        # may not resolve the deployment-default hostname).
        "git_http_base": str(engine.get("git_http_base") or "") or None,
        "registry_credential_ref": (
            str(repo.registry_credential_ref) if repo.registry_credential_ref else None
        ),
        "tenant_id": str(tenant_id),
    }


def _delegation_phase() -> PhaseSpec:
    """A minimal phase carrying just what delegation needs: the ``delegate_work_item`` tool
    (so the allowlist check passes) and a visibility/budget for the brief assembly."""
    return PhaseSpec(
        label_key="phase.turn",
        actors=[],
        visibility=VisibilitySpec(
            knowledge_classes=["rules", "lore", "misc"],
            scopes=["workspace_public"],
            entity_fields="all",
            secrets="none",
        ),
        budget=BudgetSpec(ratio={"rules": 0.5, "lore": 0.3, "misc": 0.2}, max_tokens=4000),
        tools=["delegate_work_item"],
    )


async def _load_assignee(tenant_id: uuid.UUID, persona_id: str | None) -> Any | None:
    """The assigned dev persona (id, name, principal_id) -- None when unassigned."""
    if not persona_id:
        return None
    from core.agents.models import Persona

    async with tenant_scope(tenant_id) as session:
        persona = await session.get(Persona, uuid.UUID(str(persona_id)))
        if persona is not None:
            session.expunge(persona)
        return persona


async def _codegen_profile_for(tenant_id: uuid.UUID, assignee: Any | None) -> dict[str, Any] | None:
    """The assigned persona's model connection as the codegen -- the model that writes
    the code is tenant data (a persona's connection), never deployment config. Only the
    opaque credential_ref is persisted in call arguments; the transport resolves the
    key. The echo test double is excluded (its scaffold fallback is the test contract)."""
    if assignee is None:
        return None
    from core.agents.authoring import get_agent

    profile = await get_agent(tenant_id, assignee.agent_id)
    if profile is None or profile.provider == "echo":
        return None
    return {
        "model": f"{profile.provider}/{profile.model}",
        "api_base": profile.api_base,
        "params": dict(profile.params or {}),
        "credential_ref": profile.credential_ref,
        "tenant_id": str(tenant_id),
    }


async def _supervisor_codegen_profile(
    tenant_id: uuid.UUID, session_id: uuid.UUID
) -> dict[str, Any] | None:
    """Unassigned work codes on the session supervisor's connection (there is always a
    supervisor behind a delegation) -- the last resort before the scaffold."""
    try:
        from worker.review import _session_supervisor

        persona, profile = await _session_supervisor(tenant_id, session_id)
    except ValueError:
        return None
    if profile.provider == "echo":
        return None
    return {
        "model": f"{profile.provider}/{profile.model}",
        "api_base": profile.api_base,
        "params": dict(profile.params or {}),
        "credential_ref": profile.credential_ref,
        "tenant_id": str(tenant_id),
    }


async def _load_principal(tenant_id: uuid.UUID, principal_id: uuid.UUID) -> Principal:
    async with tenant_scope(tenant_id) as session:
        principal = await session.get(Principal, principal_id)
        if principal is None:
            raise ValueError(f"no principal {principal_id}")
        session.expunge(principal)
        return principal


async def handle_delegate_work_item(payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = uuid.UUID(payload["tenant_id"])
    from core.usage_limits import UsageLimitExceededError, ensure_within_limits

    try:
        await ensure_within_limits(tenant_id)
    except UsageLimitExceededError as exc:
        with contextlib.suppress(Exception):
            await post_note(
                tenant_id,
                uuid.UUID(payload["session_id"]),
                uuid.UUID(payload["viewer_principal_id"]),
                f"\u23f8\ufe0f Delegation on hold: {exc}",
            )
        return {"work_item_id": payload["work_item_id"], "skipped": "usage_limit"}
    viewer = await _load_principal(tenant_id, uuid.UUID(payload["viewer_principal_id"]))
    environment = await _environment_config(tenant_id, payload.get("repo_id"))
    assignee = await _load_assignee(tenant_id, payload.get("assignee_persona_id"))
    entity = await get_entity(tenant_id, uuid.UUID(payload["work_item_id"]))
    extra: dict[str, Any] = {}
    if environment:
        # Attribution for the exec-environment registry + the transcript's system line:
        # which actor's work runs in the environment, for which task, in which session.
        environment.update(
            {
                "session_id": payload["session_id"],
                "repo_id": payload.get("repo_id"),
                "actor_persona_id": str(assignee.id) if assignee else None,
                "actor_label": assignee.name if assignee else "(supervisor)",
                "task_name": entity.name if entity is not None else "",
            }
        )
        extra["environment"] = environment
    codegen_profile = await _codegen_profile_for(tenant_id, assignee)
    if codegen_profile is None:
        codegen_profile = await _supervisor_codegen_profile(
            tenant_id, uuid.UUID(payload["session_id"])
        )
    if codegen_profile:
        extra["codegen_profile"] = codegen_profile
    if payload.get("repo_id"):
        repo = await get_repo(tenant_id, uuid.UUID(str(payload["repo_id"])))
        if repo is not None:
            # Separate from `remote`, which is None for a store-only repository: the
            # branch matters whether or not there is anywhere to push back to.
            extra["base_branch"] = repo.default_branch or "main"
            remote = _remote_config(tenant_id, repo)
            if remote:
                extra["remote"] = remote
    result = await delegate_work_item(
        tenant_id,
        uuid.UUID(payload["workspace_id"]),
        uuid.UUID(payload["session_id"]),
        int(payload["event_seq"]),
        viewer,
        _delegation_phase(),
        uuid.UUID(payload["work_item_id"]),
        server_key=str(payload["server_key"]),
        transport=get_mcp_transport(),
        permission_service=get_permission_service(),
        # A caller may still name the walk explicitly; the default is the one the
        # work_item lifecycle actually declares, from wherever the item currently sits.
        on_dispatch_triggers=tuple(
            payload.get("on_dispatch_triggers") or ("refine", "start", "submit_for_review")
        ),
        extra_arguments=extra or None,
    )
    outcome = result.outcome

    # Make the outcome visible where humans look (transcript), and hand the PR to the
    # facilitator's review loop. Both best-effort: the delegation itself succeeded.
    skey = await _store_key_for(tenant_id, payload.get("repo_id"))
    session_id = uuid.UUID(payload["session_id"])
    title = entity.name if entity is not None else payload["work_item_id"]
    # The note is the assigned dev's, not the facilitator's -- the transcript should show
    # who did the work. The assignment is also stamped onto the PR record so the rework
    # path attributes its fix notes to the same dev.
    author_principal = viewer.id
    if assignee is not None:
        author_principal = assignee.principal_id
        if skey is not None:
            with contextlib.suppress(GitStoreError):
                store = GitStore(default_git_root())
                record = await store.get_pr(skey, result.branch) or {}
                record.update(
                    {
                        "assignee_persona_id": str(assignee.id),
                        "assignee_name": assignee.name,
                        "assignee_principal_id": str(assignee.principal_id),
                    }
                )
                await store.record_pr(skey, result.branch, record)
    try:
        summary = await _pr_summary_line(skey, result.branch)
        await post_note(
            tenant_id,
            session_id,
            author_principal,
            f"🔀 **{title}**: Opened {summary}",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("delegation.note_failed", branch=result.branch, error=str(exc))

    if result.dispatched and skey is not None and payload.get("auto_review", True):
        await get_job_queue().enqueue(
            tenant_id,
            "facilitator_review",
            {
                "tenant_id": str(tenant_id),
                "workspace_id": payload["workspace_id"],
                "session_id": payload["session_id"],
                "work_item_id": payload["work_item_id"],
                "branch": result.branch,
                "store": skey,
                "server_key": payload["server_key"],
                "repo_id": payload.get("repo_id"),
                "review_round": 1,
            },
        )

    return {
        "work_item_id": payload["work_item_id"],
        "branch": result.branch,
        "pr_ref": outcome.pr_ref if outcome else None,
        "ci_status": outcome.ci_status if outcome else None,
        "dispatched": result.dispatched,
        "reconciled": result.reconciled,
        "transitions": result.transitions,
    }


async def handle_rework_work_item(payload: dict[str, Any]) -> dict[str, Any]:
    """Review->fix: append a commit to the existing branch/PR and drive ``rework``."""
    tenant_id = uuid.UUID(payload["tenant_id"])
    workspace_id = uuid.UUID(payload["workspace_id"])
    work_item_id = uuid.UUID(payload["work_item_id"])
    branch = str(payload["branch"])
    comment = str(payload.get("comment") or "Please address review feedback.")
    server_key = str(payload["server_key"])
    repo = str(payload["repo"])
    viewer_principal_id = uuid.UUID(payload["viewer_principal_id"])

    entity = await get_entity(tenant_id, work_item_id)
    if entity is None:
        raise ValueError(f"no work item {work_item_id}")
    work_item_arg = {
        "id": str(entity.id),
        "key": entity.key,
        "name": entity.name,
        "fields": dict(entity.data),
        "states": dict(entity.fsm_states),
    }
    environment = await _environment_config(tenant_id, payload.get("repo_id"))
    arguments: dict[str, Any] = {"branch": branch, "brief": comment, "work_item": work_item_arg}
    record_for_codegen = await GitStore(default_git_root()).get_pr(repo, branch) or {}
    rework_assignee = await _load_assignee(tenant_id, record_for_codegen.get("assignee_persona_id"))
    if environment:
        environment.update(
            {
                "session_id": payload.get("session_id"),
                "repo_id": payload.get("repo_id"),
                "actor_persona_id": str(rework_assignee.id) if rework_assignee else None,
                "actor_label": rework_assignee.name if rework_assignee else "(supervisor)",
                "task_name": entity.name,
            }
        )
        arguments["environment"] = environment
    codegen_profile = await _codegen_profile_for(tenant_id, rework_assignee)
    if codegen_profile is None and payload.get("session_id"):
        codegen_profile = await _supervisor_codegen_profile(
            tenant_id, uuid.UUID(str(payload["session_id"]))
        )
    if codegen_profile:
        arguments["codegen_profile"] = codegen_profile
    if payload.get("repo_id"):
        repo_row = await get_repo(tenant_id, uuid.UUID(str(payload["repo_id"])))
        if repo_row is not None:
            remote = _remote_config(tenant_id, repo_row)
            if remote:
                arguments["remote"] = remote
    transport = get_mcp_transport()
    result = await transport.call_tool(
        McpServerRef(key=server_key, url=repo), "delegate_work_item", arguments
    )
    review_round = int(payload.get("review_round", 0))
    # Drive changes_requested -> in_progress -> back to in_review: the fix commit is on
    # the branch, so the item is reviewable again (by the facilitator loop or a human).
    await transition(
        viewer_principal_id,
        tenant_id,
        workspace_id,
        work_item_id,
        "lifecycle",
        "rework",
        idempotency_key=f"rework:{work_item_id}:{branch}:{review_round}",
        permission_service=get_permission_service(),
        cause="agent",
    )
    await transition(
        viewer_principal_id,
        tenant_id,
        workspace_id,
        work_item_id,
        "lifecycle",
        "submit_for_review",
        idempotency_key=f"resubmit:{work_item_id}:{branch}:{review_round}",
        permission_service=get_permission_service(),
        cause="agent",
    )

    session_id = payload.get("session_id")
    if session_id:
        # The fix note carries the assigned dev's name (stamped on the PR record at
        # delegation time), falling back to the dispatching supervisor.
        note_author = viewer_principal_id
        with contextlib.suppress(GitStoreError, ValueError):
            record = await GitStore(default_git_root()).get_pr(repo, branch) or {}
            if record.get("assignee_principal_id"):
                note_author = uuid.UUID(str(record["assignee_principal_id"]))
        try:
            summary = await _pr_summary_line(repo, branch)
            await post_note(
                tenant_id,
                uuid.UUID(str(session_id)),
                note_author,
                f"🛠️ Fix pushed for **{entity.name}** → {summary}",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("rework.note_failed", branch=branch, error=str(exc))
        # Chain the next review round (auto-review path only), bounded.
        rounds_allowed = await max_review_rounds(tenant_id, workspace_id)
        if 0 < review_round < rounds_allowed:
            await get_job_queue().enqueue(
                tenant_id,
                "facilitator_review",
                {
                    "tenant_id": str(tenant_id),
                    "workspace_id": str(workspace_id),
                    "session_id": str(session_id),
                    "work_item_id": str(work_item_id),
                    "branch": branch,
                    "store": repo,
                    "server_key": server_key,
                    "repo_id": payload.get("repo_id"),
                    "review_round": review_round + 1,
                },
            )
        elif review_round >= rounds_allowed:
            await post_note(
                tenant_id,
                uuid.UUID(str(session_id)),
                viewer_principal_id,
                f"⏸️ **{entity.name}**: review round limit reached "
                f"({rounds_allowed}) — leaving in review for a human decision.",
            )

    return {
        "work_item_id": str(work_item_id),
        "branch": branch,
        "pr_ref": result.structured.get("pr_ref"),
        "ci_status": result.structured.get("ci_status"),
        "commits": result.structured.get("commits"),
    }


async def handle_teardown_session_envs(payload: dict[str, Any]) -> dict[str, Any]:
    """Session archived -> remove its exec environments (named pyr-env-<session8>-*)
    across EVERY declared engine -- the session may have run on any of them."""
    from core.exec_engines import declared_engines

    session8 = str(payload["session_id"])[:8]
    removed = 0
    for engine in declared_engines() or [{"key": None}]:
        provider = get_exec_env_provider(engine.get("key"))
        removed += await provider.teardown_matching(f"pyr-env-{session8}-")
    if payload.get("tenant_id"):
        from core.exec_envs import mark_removed_by_prefix

        with contextlib.suppress(Exception):
            await mark_removed_by_prefix(
                uuid.UUID(str(payload["tenant_id"])), f"pyr-env-{session8}-"
            )
    return {"removed": removed}


async def handle_kill_exec_environment(payload: dict[str, Any]) -> dict[str, Any]:
    """A human asked (Repos page / POST /exec-environments/{id}/kill): tear the
    environment down through its engine and record the outcome. Idempotent -- an
    already-gone environment still ends up 'killed' (that IS the requested state)."""
    from core.exec_envs import get_environment, set_status

    tenant_id = uuid.UUID(payload["tenant_id"])
    environment_id = uuid.UUID(payload["environment_id"])
    row = await get_environment(tenant_id, environment_id)
    if row is None:
        return {"environment_id": str(environment_id), "outcome": "not_found"}
    provider = get_exec_env_provider(row.engine_key)
    removed = await provider.teardown_matching(row.name)
    await set_status(tenant_id, environment_id, "killed")
    return {"environment_id": str(environment_id), "name": row.name, "removed": removed}
