"""Worker jobs for delegated coding work.

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
from collections.abc import Mapping
from typing import Any

import structlog
from pydantic import BaseModel

from adapters.mcp.git_store import GitStore, GitStoreError, default_git_root
from core.actions.delegation import delegate_work_item
from core.entities.mutation import transition
from core.entities.storage import get_entity
from core.ports.mcp import McpServerRef
from core.process.dsl.schema import BudgetSpec, PhaseSpec, VisibilitySpec
from core.repos.service import get_repo, resolve_git_identity, store_key
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


async def _remote_config(
    tenant_id: uuid.UUID, repo: Any, persona_id: uuid.UUID | None = None
) -> dict[str, Any] | None:
    """The push-back target: the registration's source_url + an opaque credential_ref
    (the transport resolves the token itself; nothing secret is persisted in job
    payloads or action records).

    Which credential is ``resolve_git_identity``'s decision, not this function's. It
    used to pass the repository's own ``credential_ref`` unconditionally, so every
    branch and pull request a delegated agent opened appeared under the repository
    bot -- while the *merge* path, which did ask, arrived as the persona. The same
    agent therefore pushed as one identity and merged as another, and the per-persona
    bindings the UI offers had no effect on the work they were bound for.
    """
    url = (repo.source_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    credential_ref = await resolve_git_identity(tenant_id, repo.id, persona_id)
    return {
        "url": url,
        "provider": repo.provider,
        "credential_ref": str(credential_ref) if credential_ref else None,
        "tenant_id": str(tenant_id),
    }


async def _environment_config(
    tenant_id: uuid.UUID, repo_id: str | None, base_branch: str | None = None
) -> dict[str, Any] | None:
    """The exec-environment config for a delegation.

    Three layers, folded per field by ``core.repos.build_recipe``: the repo row (an
    operator's override), ``pyrrhula-build.json`` at the ref being worked (the recipe
    that lives with the code), and the runtime catalog the tenant can now extend
    itself. None (-> direct-store fallback) when no repo is bound to the call."""
    if not repo_id:
        return None
    repo = await get_repo(tenant_id, uuid.UUID(repo_id))
    if repo is None:
        return None

    from core.repos.build_recipe import (
        BuildRecipeError,
        read_repo_manifest,
        resolve_build_recipe,
    )
    from core.repos.runtimes import get_runtime

    ref = base_branch or repo.default_branch or "main"
    try:
        manifest = await read_repo_manifest(store_key(tenant_id, repo.key), ref=ref)
    except BuildRecipeError as exc:
        # A manifest that is present and wrong must not fall back to the row silently --
        # the author configured something and would never learn it was ignored.
        log.warning(
            "delegation.build_manifest_invalid", repo=repo.key, ref=ref, error=str(exc)[:300]
        )
        raise

    manifest_runtime = manifest.get("runtime")
    recipe = resolve_build_recipe(
        repo_runtime=repo.runtime,
        repo_image=repo.runtime_image,
        repo_setup_cmds=list(repo.setup_cmds or []),
        repo_test_cmd=repo.test_cmd,
        repo_build_cmd=repo.build_cmd,
        repo_artifact_name=repo.artifact_name,
        manifest=manifest,
        runtime_entry=await get_runtime(tenant_id, repo.runtime),
        manifest_runtime_entry=(
            await get_runtime(tenant_id, str(manifest_runtime)) if manifest_runtime else None
        ),
    )
    if any(src == "manifest" for src in recipe.sources.values()):
        log.info(
            "delegation.build_recipe",
            repo=repo.key,
            ref=ref,
            sources=recipe.sources,
        )

    from core.exec_engines import engine_by_key, get_tenant_engine_key

    engine_key = await get_tenant_engine_key(tenant_id)
    engine = engine_by_key(engine_key) or {}
    return {
        "image": recipe.image,
        "setup_cmds": recipe.setup_cmds,
        "test_cmd": recipe.test_cmd,
        "build_cmd": recipe.build_cmd,
        "artifact_name": recipe.artifact_name,
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


async def _assignable_devs(tenant_id: uuid.UUID, session_id: uuid.UUID) -> list[Any]:
    """The session's non-supervisor roster -- the people work can be given to."""
    from core.agents.models import Persona
    from core.sessions.lifecycle import list_session_roster

    entries = [e for e in await list_session_roster(tenant_id, session_id) if not e.is_supervisor]
    devs: list[Any] = []
    async with tenant_scope(tenant_id) as session:
        for entry in entries:
            persona = await session.get(Persona, entry.persona_id)
            if persona is None:
                continue
            session.expunge(persona)
            devs.append(persona)
    return devs


class _AssigneeChoice(BaseModel):
    """Which dev the facilitator picks, and why."""

    assignee: str
    reason: str = ""


_ASSIGN_SYSTEM = (
    "You are the technical lead of a software team, assigning one work item to one "
    "developer. Match the work to the tier: routine, well-specified, low-risk changes "
    "go to the most junior person who can do them; work needing design judgement, or "
    "touching something many other things depend on, goes to a senior one. Do not "
    "default to the most senior available -- that wastes the team. Answer with a name "
    "from the roster exactly as written, and one sentence of reasoning."
)


async def _facilitator_assignee(
    tenant_id: uuid.UUID, session_id: uuid.UUID, work_item: Any, devs: list[Any]
) -> tuple[Any | None, str]:
    """Ask the session's supervisor which developer should take this work item.

    The roster mechanism has always let a supervisor name the assignee; nothing ever
    asked it one, so an unassigned item fell to a round robin over persona ids -- which
    is not a delegation decision, it is an ordering. A lead that can reason about
    seniority should be doing this, and the tiered roster exists precisely so the answer
    can differ per item.

    Best-effort: any failure returns no choice and the caller keeps its round robin,
    because an unavailable model must not stall the work.
    """
    if not devs:
        return None, ""
    from api.encryptor_factory import get_encryptor
    from api.model_provider_factory import get_model_provider
    from core.agents.authoring import resolve_connection_api_key
    from core.ports.model_provider import GenerationRequest
    from worker.review import _session_supervisor

    try:
        persona, profile = await _session_supervisor(tenant_id, session_id)
    except Exception:  # noqa: BLE001 -- no supervisor resolvable; caller falls back
        return None, ""

    fields = dict(getattr(work_item, "data", None) or {})
    roster = "\n".join(
        f"- {d.name}" + (f": {d.description}" if getattr(d, "description", "") else "")
        for d in devs
    )
    req = GenerationRequest(
        model=f"{profile.provider}/{profile.model}",
        messages=[
            {"role": "system", "content": _ASSIGN_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Work item: {fields.get('title') or getattr(work_item, 'name', '')}\n"
                    f"{str(fields.get('description') or '')[:2000]}\n\n"
                    f"Roster:\n{roster}"
                ),
            },
        ],
        purpose="delegation",
        max_tokens=300,
        api_base=profile.api_base,
        params=dict(profile.params or {}),
        api_key=await resolve_connection_api_key(
            tenant_id, profile.credential_ref, encryptor=get_encryptor()
        ),
    )
    try:
        choice = await get_model_provider(profile.provider).generate_structured(
            req, _AssigneeChoice
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("delegation.assignee_choice_failed", error=str(exc)[:200])
        return None, ""

    by_name = {d.name.strip().lower(): d for d in devs}
    chosen = by_name.get(choice.assignee.strip().lower())
    if chosen is None:
        log.warning("delegation.assignee_choice_unknown", named=choice.assignee[:60])
        return None, ""
    log.info(
        "delegation.assignee_chosen",
        by=persona.name,
        assignee=chosen.name,
        reason=choice.reason[:160],
    )
    return chosen, choice.reason


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


def _run_timeout(engine: dict[str, Any] | None = None) -> int:
    """How long the environment may run, and so how long its inference token is good for.

    Matches the exec-env adapters' own default (``run_timeout_seconds``, 1800) so a token
    does not expire while the container that holds it is still working -- and does not
    outlive it either.
    """
    return int((engine or {}).get("run_timeout_seconds") or 1800)


async def _harness_config(
    tenant_id: uuid.UUID, assignee: Any | None, run_timeout_seconds: int
) -> dict[str, Any]:
    """What the environment needs to run this persona's harness, or ``{}`` for none.

    Resolved here rather than trusted from the persona row: a persona names a *key*, and
    a tenant that has withheld that harness -- internal policy, a licence it may not
    use -- must stop getting it without anyone editing personas. Same reading as
    ``core.mcp.client.available_tools``, which re-derives rather than believing what was
    offered earlier.

    Falling back to no harness (and so to the one-shot codegen path) is deliberate: a
    withdrawn harness should degrade the work, not fail the delegation.
    """
    if assignee is None or not getattr(assignee, "harness", ""):
        return {}
    from core.agents.authoring import get_agent
    from core.harness.registry import get_harness

    spec = await get_harness(tenant_id, assignee.harness)
    if spec is None:
        log.warning(
            "delegation.harness_unavailable",
            harness=assignee.harness,
            persona=str(assignee.id),
            detail="not registered for this tenant, or withheld -- falling back to codegen",
        )
        return {}
    profile = await get_agent(tenant_id, assignee.agent_id)
    if profile is None:
        return {}
    return {
        "harness": {"key": assignee.harness, **spec},
        # The connection the inference token will bind to, and the model the proxy will
        # actually call. The harness is told this name; the proxy uses it regardless.
        "harness_agent_id": str(profile.id),
        "harness_model": profile.model,
        # The token must not outlive the container that holds it.
        "harness_ttl_seconds": run_timeout_seconds,
    }


def _apply_harness(environment: dict[str, Any], harness_cfg: dict[str, Any]) -> None:
    """Fold a harness into an environment recipe, in place.

    Its setup commands are APPENDED, never substituted: the repo still needs its own
    toolchain, because the harness has to run that repo's tests. Its image, when the spec
    names one, does win -- that is the pre-baked variant an operator built precisely so a
    one-shot engine stops reinstalling the harness on every run.
    """
    if not harness_cfg:
        return
    spec = harness_cfg.get("harness") or {}
    environment.update(harness_cfg)
    if spec.get("image"):
        environment["image"] = spec["image"]
    environment["setup_cmds"] = [
        *(environment.get("setup_cmds") or []),
        *(spec.get("setup_cmds") or []),
    ]


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
    environment = await _environment_config(
        tenant_id, payload.get("repo_id"), payload.get("base_branch")
    )
    assignee = await _load_assignee(tenant_id, payload.get("assignee_persona_id"))
    entity = await get_entity(tenant_id, uuid.UUID(payload["work_item_id"]))
    # An unassigned item used to fall to a round robin over persona ids. Ask the
    # facilitator instead -- it is the one that can weigh the work against the roster,
    # and a tiered roster is pointless if the choice is positional. Only when nothing
    # was named: an explicit assignee is a decision already made.
    if assignee is None and entity is not None:
        devs = await _assignable_devs(tenant_id, uuid.UUID(payload["session_id"]))
        chosen, reason = await _facilitator_assignee(
            tenant_id, uuid.UUID(payload["session_id"]), entity, devs
        )
        if chosen is None and devs:
            # The facilitator could not answer (model down, unparseable, a name that is
            # not on the roster). Work still has to go to somebody, so this is where the
            # old round robin lives now -- as a fallback, not as the decision.
            chosen = devs[0]
            reason = ""
        if chosen is not None:
            assignee = chosen
            with contextlib.suppress(Exception):
                await post_note(
                    tenant_id,
                    uuid.UUID(payload["session_id"]),
                    uuid.UUID(payload["viewer_principal_id"]),
                    f"\U0001f9ed Assigned to {chosen.name}" + (f" — {reason}" if reason else ""),
                )
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
        _apply_harness(environment, await _harness_config(tenant_id, assignee, _run_timeout()))
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
            remote = await _remote_config(
                tenant_id, repo, assignee.id if assignee is not None else None
            )
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
                # Carried explicitly: a delegation may have been told to branch from
                # something other than the repo's default, and the review has to diff
                # against whatever the work was actually branched from.
                "base_branch": payload.get("base_branch"),
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


# The recorded output is already noise-stripped and trimmed at the source, so this is
# a second trim of a curated record -- and it must take the SAME end the first one
# favoured. Taking the head here handed the agent 32,000 characters that named not one
# failing test, because a run prints its failures and its tally last.
_REWORK_TEST_OUTPUT_CHARS = 32000


def _with_test_output(comment: str, pr: Mapping[str, Any]) -> str:
    """Append what the tests actually printed to the rework brief.

    Older pull-request records predate ``test_output`` and carry only ``summary``; fall
    back to that rather than to nothing, and say plainly when there is no recorded run at
    all -- an agent that believes the suite was green when nobody ran it will "fix"
    whatever it feels like.
    """
    status = str(pr.get("ci_status") or "").strip().lower()
    if status == "passed":
        return f"{comment}\n\nBuild: the tests PASSED on this branch."
    if status not in ("failed", "error"):
        return f"{comment}\n\nBuild: no test result was recorded for this branch."
    detail = str(pr.get("test_output") or pr.get("summary") or "").strip()
    if not detail:
        return f"{comment}\n\nBuild: the tests FAILED on this branch; no output was captured."
    return (
        f"{comment}\n\nBuild: the tests FAILED on this branch. This is what they printed "
        f"-- fix what it shows rather than guessing at it. You cannot run the suite "
        f"yourself, so this output is the only record of what the code actually "
        f"produced:\n\n{detail[-_REWORK_TEST_OUTPUT_CHARS:]}"
    )


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
    environment = await _environment_config(
        tenant_id, payload.get("repo_id"), payload.get("base_branch")
    )
    record_for_codegen = await GitStore(default_git_root()).get_pr(repo, branch) or {}
    # The agent cannot run the suite -- it emits file contents and the environment runs
    # the tests -- so unless the failure is put in front of it, it is being asked to fix
    # something it has never seen. That is not a subtle handicap: every attempt at
    # loxia's snapshot fixtures hand-wrote a guess, because a guess was the only thing
    # available. The reviewer's prose says a test is red; this says how.
    comment = _with_test_output(comment, record_for_codegen)
    arguments: dict[str, Any] = {"branch": branch, "brief": comment, "work_item": work_item_arg}
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
        # Rework runs through the same harness the first attempt did -- resolved again
        # rather than remembered, so a harness withdrawn between rounds stops being used.
        _apply_harness(
            environment, await _harness_config(tenant_id, rework_assignee, _run_timeout())
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
            # The rework path acts as the same persona that did the original work.
            remote = await _remote_config(
                tenant_id, repo_row, rework_assignee.id if rework_assignee else None
            )
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
                    "base_branch": payload.get("base_branch"),
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
