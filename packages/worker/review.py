"""Facilitator PR review (`facilitator_review`): the loop the human was doing by hand.

After a delegation lands a PR, this job has the session's supervisor persona read the
branch diff and give a structured verdict. Approve drives the work item's ``approve``
transition; request-changes drives ``request_changes`` and enqueues the existing
``rework_work_item`` job with the review comments — whose completion re-submits the item
for review and chains the next round, bounded by the workspace's ``max_review_rounds``
setting (default 2, under the deployment's ``PYRRHULA_REVIEW_ROUNDS_CEILING``). Every
verdict is posted into the session transcript, so the whole loop is
visible where the humans are looking.

A model failure degrades to a transcript note ("manual review needed") with the item left
``in_review`` — never a crashed job, never a silently stuck item.
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from typing import Any

import structlog
from pydantic import BaseModel

from adapters.gitremote.base import ReviewVerdict as ReviewVerdictLiteral
from adapters.gitremote.registry import resolve_remote
from adapters.mcp.git_store import GitStore, GitStoreError, default_git_root
from core.agents.authoring import resolve_connection_api_key
from core.agents.models import Agent, Persona
from core.audit.models import UsageRecordRow
from core.entities.mutation import transition
from core.entities.storage import get_entity
from core.ports.model_provider import GenerationRequest
from core.repos.service import get_repo, resolve_git_identity
from core.sessions.lifecycle import list_session_roster
from core.tenancy.egress import load_egress_policy
from core.tenancy.scope import tenant_scope
from worker.encryptor_factory import get_encryptor
from worker.job_queue_factory import get_job_queue
from worker.model_provider_factory import get_model_provider
from worker.permission_service_factory import get_permission_service
from worker.transcript import post_note

log = structlog.get_logger()

_PURPOSE = "delegation"

_REVIEW_SYSTEM = (
    "You are the tech lead reviewing a pull request from a coding agent. Judge only what "
    "the diff shows against the work item's requirements. Verdict rules: 'approve' when "
    "the change plausibly implements the work item and has no obvious defect; "
    "'request_changes' only for concrete, actionable problems (bugs, missing "
    "requirements, wrong language/framework) — style nits alone never block. When "
    "requesting changes, write comments a coding agent can act on directly: name the "
    "file and what to change.\n\n"
    "The diff begins with a COMPLETE list of every changed file, and that list is never "
    "truncated. File contents below it may be. Judge scope -- what was touched, and "
    "whether anything was touched that should not have been -- from the list, which is "
    "whole; judge correctness from the contents you can see. Never treat a truncated "
    "body as evidence that nothing else changed, and do not withhold a verdict merely "
    "because the contents are cut: say which part of your judgement the truncation "
    "limits.\n\n"
    "The build result for this branch is stated above the diff, and it outranks how "
    "reasonable the diff looks. Decide against the work item's own acceptance "
    "condition: if the test or behaviour THIS item exists to fix is still failing, the "
    "verdict is 'request_changes' however scope-correct the change is -- approving that "
    "is the one review error nothing downstream can catch, because the merge order is "
    "planned from approvals.\n\n"
    "Judge the remaining failures rather than counting them. Some suites are already red "
    "before the change lands, and a failure this item was never asked to fix, and that "
    "the diff cannot plausibly have caused, is not grounds to refuse it -- name those, "
    "say they are pre-existing and out of scope, and let the item stand or fall on its "
    "own condition. A failure the diff could have caused is this item's problem whether "
    "or not it was named in the work item. Always list the failing tests you were given "
    "and say which of the two they are."
)


def _build_line(pr: dict[str, Any]) -> str:
    """What the branch's tests did, stated where the reviewer cannot miss it.

    The reviewer was given the work item and the diff and nothing else, so every verdict
    it ever reached was a reading of the diff. That is fine for finding defects and
    useless for the one rule the sample exists to demonstrate: it approved a
    scope-correct one-file change on a branch whose suite was red, because nothing in
    front of it said the suite was red.

    States the result and leaves the judgement to the prompt. An earlier version said
    "This is request_changes" here, which is right for the failure that matters and
    wrong for every other one: a suite that is already red on the trunk would then make
    every work item against it unapprovable forever, including one that does fix its
    own test.
    """
    status = str(pr.get("ci_status") or "").strip().lower()
    if status == "passed":
        return "Build: tests PASSED on this branch."
    if status in ("failed", "error"):
        # `test_output` is what the run printed, recorded whole; `summary` is the short
        # label and is all that older records carry. Fall back to it rather than to an
        # empty string that reads as "no detail available".
        detail = str(pr.get("test_output") or pr.get("summary") or "").strip()
        tail = f"\n{detail[:2500]}" if detail else ""
        return (
            "Build: tests FAILED on this branch. Check whether this work item's own "
            "target is among the failures before deciding." + tail
        )
    return "Build: no test result recorded for this branch — say so in your verdict."


_MERGE_ORDER_SYSTEM = (
    "You are the tech lead planning how to land several pull requests from one delivery "
    "batch. Recommend the order to merge them, considering dependencies (scaffolding and "
    "shared infrastructure before features that build on it) and likely conflicts "
    "(branches touching the same files merge sooner). Return the PR numbers in merge "
    "order plus a short rationale (2-4 sentences)."
)


# The deployment's default, and its ceiling. How many times a reviewer may send work
# back is workflow policy -- the same kind of decision as how many rounds an interrogation
# runs -- so a workspace or tenant sets its own. The ceiling stays with the deployment
# because an unbounded review loop spends a tenant's API budget in a cycle nobody watched.
_DEFAULT_REVIEW_ROUNDS = 2
_MAX_REVIEW_ROUNDS_CEILING = int(os.environ.get("PYRRHULA_REVIEW_ROUNDS_CEILING", "10"))
REVIEW_ROUNDS_KEY = "max_review_rounds"


async def max_review_rounds(tenant_id: uuid.UUID, workspace_id: uuid.UUID | None = None) -> int:
    """Rounds this workspace allows, clamped to the deployment's ceiling."""
    from core.settings.resolve import resolved_setting

    chosen = await resolved_setting(
        tenant_id, workspace_id, REVIEW_ROUNDS_KEY, _DEFAULT_REVIEW_ROUNDS
    )
    try:
        rounds = int(chosen)
    except (TypeError, ValueError):
        rounds = _DEFAULT_REVIEW_ROUNDS
    return max(0, min(rounds, _MAX_REVIEW_ROUNDS_CEILING))


class ReviewVerdict(BaseModel):
    verdict: str  # 'approve' | 'request_changes'
    comments: str


class MergePlan(BaseModel):
    order: list[str]  # PR refs (e.g. "#11"), first = merge first
    rationale: str


async def _session_supervisor(tenant_id: uuid.UUID, session_id: uuid.UUID) -> tuple[Persona, Agent]:
    for entry in await list_session_roster(tenant_id, session_id):
        if entry.is_supervisor:
            async with tenant_scope(tenant_id) as session:
                persona = await session.get(Persona, entry.persona_id)
                if persona is None:
                    break
                profile = await session.get(Agent, persona.agent_id)
                if profile is None:
                    break
                session.expunge(persona)
                session.expunge(profile)
                return persona, profile
    raise ValueError(f"session {session_id} has no supervisor persona to review as")


async def _post_remote_review(
    tenant_id: uuid.UUID,
    repo_id: str | None,
    reviewer_persona_id: uuid.UUID,
    pr: dict[str, Any],
    *,
    approve: bool,
    body: str,
) -> None:
    """Mirror the verdict onto the remote PR/MR under the REVIEWER'S OWN identity (G4.17).

    Resolves the reviewer persona's bound git credential (falling back to the repo's
    default) and files a FORMAL review — approve / request_changes — so the host's merge
    gate can see it. If the formal review is refused (GitHub 422s a review from the PR's
    own author, which happens when the reviewer shares the repo default token with the bot
    that opened the PR), it degrades to a plain comment — never a crash, and the verdict is
    still on the PR. Generic remotes have no review API and land as a comment (or nothing).
    """
    if not repo_id or not pr.get("html_url"):
        return
    number_raw = str(pr.get("pr_ref") or "").lstrip("#")
    if not number_raw.isdigit():
        return
    repo = await get_repo(tenant_id, uuid.UUID(str(repo_id)))
    if repo is None or not repo.source_url:
        return
    credential_ref = await resolve_git_identity(
        tenant_id, uuid.UUID(str(repo_id)), reviewer_persona_id
    )
    if credential_ref is None:
        return
    remote = resolve_remote(repo.source_url, repo.provider)
    if remote is None:
        return
    token = await resolve_connection_api_key(
        tenant_id, str(credential_ref), encryptor=get_encryptor()
    )
    if not token:
        return
    number = int(number_raw)
    verdict: ReviewVerdictLiteral = "approve" if approve else "request_changes"
    filed = await remote.submit_review(
        repo.source_url, token, number=number, verdict=verdict, body=body
    )
    if filed:
        return
    # Formal review refused (or unsupported) -> the verdict still belongs on the PR.
    posted = await remote.post_pr_comment(repo.source_url, token, number=number, body=body)
    if not posted:
        log.warning("facilitator_review.remote_review_failed", pr=pr.get("pr_ref"))


async def _automerge_allowed(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
    """Whether an agent may merge a PR itself, or merging is a human's job (the default).

    Off by default: a merge is consequential and the conservative posture is that a person
    presses it. A workspace opts in with the `allow_automerge` setting."""
    from core.tenancy.models import Workspace

    async with tenant_scope(tenant_id) as session:
        ws = await session.get(Workspace, workspace_id)
        return bool((ws.settings or {}).get("allow_automerge")) if ws is not None else False


async def _merge_remote(
    tenant_id: uuid.UUID, repo_id: str | None, merger_persona_id: uuid.UUID, pr: dict[str, Any]
) -> bool:
    """Merge the hosted PR under the merger persona's own identity (G4.17). False when the
    repo has no remote, no credential resolves, or the host's gate refuses (e.g. a required
    review has not landed) -- the caller reports that, it is not an error."""
    if not repo_id or not pr.get("html_url"):
        return False
    number_raw = str(pr.get("pr_ref") or "").lstrip("#")
    if not number_raw.isdigit():
        return False
    repo = await get_repo(tenant_id, uuid.UUID(str(repo_id)))
    if repo is None or not repo.source_url:
        return False
    credential_ref = await resolve_git_identity(
        tenant_id, uuid.UUID(str(repo_id)), merger_persona_id
    )
    if credential_ref is None:
        return False
    remote = resolve_remote(repo.source_url, repo.provider)
    if remote is None:
        return False
    token = await resolve_connection_api_key(
        tenant_id, str(credential_ref), encryptor=get_encryptor()
    )
    if not token:
        return False
    return await remote.merge_pull_request(repo.source_url, token, number=int(number_raw))


async def _base_branch(
    tenant_id: uuid.UUID, repo_id: str | None, declared: str | None = None
) -> str:
    """What this repository's work is branched from.

    ``GitStore``'s diff helpers default to ``main``, which is a fine default and a wrong
    answer for any repository that calls its trunk something else. A review that cannot
    read the diff raises, the job fails, and the work item sits in ``in_review`` with no
    verdict -- so a repository whose default branch is ``master`` had a delegation loop
    that delivered pull requests nobody could approve. The delegation path already
    resolves this (``worker.delegation._environment_config``); the review path did not.
    """
    if declared:
        return str(declared)
    if repo_id:
        repo = await get_repo(tenant_id, uuid.UUID(str(repo_id)))
        if repo is not None and repo.default_branch:
            return str(repo.default_branch)
    return "main"


async def handle_facilitator_review(payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = uuid.UUID(payload["tenant_id"])
    workspace_id = uuid.UUID(payload["workspace_id"])
    session_id = uuid.UUID(payload["session_id"])
    work_item_id = uuid.UUID(payload["work_item_id"])
    branch = str(payload["branch"])
    store_key = str(payload["store"])
    review_round = int(payload.get("review_round", 1))

    persona, profile = await _session_supervisor(tenant_id, session_id)
    from core.usage_limits import UsageLimitExceededError, ensure_within_limits

    try:
        await ensure_within_limits(tenant_id, agent_id=profile.id, persona_id=persona.id)
    except UsageLimitExceededError as exc:
        await post_note(
            tenant_id,
            session_id,
            persona.principal_id,
            f"\u23f8\ufe0f Review of this PR is on hold: {exc}",
        )
        return {"work_item_id": str(work_item_id), "verdict": "limit"}
    entity = await get_entity(tenant_id, work_item_id)
    if entity is None:
        raise ValueError(f"no work item {work_item_id}")
    if entity.fsm_states.get("lifecycle") != "in_review":
        # Someone (human or an earlier round) already moved it — nothing to review.
        return {"work_item_id": str(work_item_id), "skipped": entity.fsm_states.get("lifecycle")}

    store = GitStore(default_git_root())
    base = await _base_branch(tenant_id, payload.get("repo_id"), payload.get("base_branch"))
    try:
        diff = await store.diff_text(store_key, branch, base=base)
    except GitStoreError as exc:
        raise ValueError(f"cannot read diff for {branch} against {base}: {exc}") from exc
    pr = await store.get_pr(store_key, branch) or {}
    pr_label = f"PR {pr.get('pr_ref', branch)}" + (
        f" ({pr['html_url']})" if pr.get("html_url") else ""
    )

    title = entity.name
    description = str(entity.data.get("description", ""))
    model_string = f"{profile.provider}/{profile.model}"
    req = GenerationRequest(
        egress_policy=await load_egress_policy(tenant_id),
        model=model_string,
        messages=[
            {"role": "system", "content": f"{persona.persona_md}\n\n{_REVIEW_SYSTEM}"},
            {
                "role": "user",
                "content": (
                    f"Work item: {title}\n{description}\n\n"
                    f"Review round {review_round} of "
                    f"{await max_review_rounds(tenant_id, workspace_id)}.\n\n"
                    f"{_build_line(pr)}\n\n"
                    f"Diff of branch {branch}:\n```diff\n{diff or '(empty diff)'}\n```"
                ),
            },
        ],
        purpose=_PURPOSE,
        max_tokens=800,
        api_base=profile.api_base,
        params=dict(profile.params or {}),
        api_key=await resolve_connection_api_key(
            tenant_id, profile.credential_ref, encryptor=get_encryptor()
        ),
    )
    provider = get_model_provider(profile.provider)
    start = time.monotonic()
    try:
        verdict = await provider.generate_structured(req, ReviewVerdict)
    except Exception as exc:  # noqa: BLE001 -- degrade to a visible note, keep in_review
        log.warning("facilitator_review.model_failed", branch=branch, error=str(exc))
        await post_note(
            tenant_id,
            session_id,
            persona.principal_id,
            f"⚠️ Automatic review of **{title}** ({pr_label}) failed — "
            f"manual review needed. ({str(exc)[:200]})",
        )
        return {"work_item_id": str(work_item_id), "verdict": "error"}
    latency_ms = int((time.monotonic() - start) * 1000)

    async with tenant_scope(tenant_id) as session:
        session.add(
            UsageRecordRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                persona_id=persona.id,
                agent_id=profile.id,
                provider=profile.provider,
                model=profile.model,
                purpose=_PURPOSE,
                prompt_tokens=sum(
                    provider.count_tokens(str(m.get("content") or ""), model_string)
                    for m in req.messages
                ),
                completion_tokens=provider.count_tokens(verdict.comments, model_string),
                latency_ms=latency_ms,
            )
        )

    approve = verdict.verdict.strip().lower() != "request_changes"

    # The verdict belongs on the PR too, not only in the transcript: append it to the
    # store's PR record and, when the repo syncs to GitHub, post it as a PR comment
    # (a plain comment, not a formal review -- GitHub refuses approve/request-changes
    # reviews from the PR's own author, which the platform token is). Best-effort.
    verdict_word = "APPROVED ✅" if approve else "CHANGES REQUESTED 🔁"
    with contextlib.suppress(GitStoreError):
        record = await store.get_pr(store_key, branch) or {}
        reviews = list(record.get("reviews") or [])
        reviews.append(
            {
                "round": review_round,
                "verdict": "approve" if approve else "request_changes",
                "by": persona.name,
                "comments": verdict.comments[:2000],
            }
        )
        record["reviews"] = reviews
        await store.record_pr(store_key, branch, record)
    await _post_remote_review(
        tenant_id,
        payload.get("repo_id"),
        persona.id,
        pr,
        approve=approve,
        body=(
            f"## Review (round {review_round}): {verdict_word}\n\n{verdict.comments}\n\n"
            f"— {persona.name} (Pyrrhula facilitator)"
        ),
    )

    if approve:
        await transition(
            persona.principal_id,
            tenant_id,
            workspace_id,
            work_item_id,
            "lifecycle",
            "approve",
            idempotency_key=f"approve:{work_item_id}:{branch}:{review_round}",
            permission_service=get_permission_service(),
            cause="agent",
        )
        merged = False
        if await _automerge_allowed(tenant_id, workspace_id):
            merged = await _merge_remote(tenant_id, payload.get("repo_id"), persona.id, pr)
        if merged:
            # The lifecycle has an `approved --merge--> merged` transition and nothing
            # was driving it, so a landed pull request left its work item sitting at
            # `approved` for ever: the repository said merged, the board said not, and
            # the board is what a human reads. Same idempotency shape as the approval
            # above -- a retried review must not transition twice.
            await transition(
                persona.principal_id,
                tenant_id,
                workspace_id,
                work_item_id,
                "lifecycle",
                "merge",
                idempotency_key=f"merge:{work_item_id}:{branch}",
                permission_service=get_permission_service(),
                cause="agent",
            )
        tail = (
            f"\n\n🔀 Auto-merged by {persona.name}."
            if merged
            else (
                (
                    "\n\n🔀 Ready to merge — awaiting a human (auto-merge is off for "
                    "this workspace)."
                    if not await _automerge_allowed(tenant_id, workspace_id)
                    # Auto-merge was on and the host refused. Saying so in the session is
                    # the difference between "a human still has to press it" and "someone
                    # needs to look" -- the adapter logs the host's own reason.
                    else "\n\n🔀 Approved, but the host refused the merge — see the logs "
                    "for its reason (often a missing permission on the token, a required "
                    "check, or a protected branch)."
                )
                if pr.get("html_url")
                else ""
            )
        )
        await post_note(
            tenant_id,
            session_id,
            persona.principal_id,
            f"✅ **Review (round {review_round})** — approved **{title}** ({pr_label}).\n\n"
            f"{verdict.comments}".rstrip()
            + tail,
        )
        return {"work_item_id": str(work_item_id), "verdict": "approve", "merged": merged}

    await transition(
        persona.principal_id,
        tenant_id,
        workspace_id,
        work_item_id,
        "lifecycle",
        "request_changes",
        idempotency_key=f"auto-review:{work_item_id}:{branch}:{review_round}",
        permission_service=get_permission_service(),
        cause="agent",
    )
    await post_note(
        tenant_id,
        session_id,
        persona.principal_id,
        f"🔁 **Review (round {review_round})** — requested changes on **{title}** "
        f"({pr_label}):\n\n{verdict.comments}",
    )
    await get_job_queue().enqueue(
        tenant_id,
        "rework_work_item",
        {
            "tenant_id": str(tenant_id),
            "workspace_id": str(workspace_id),
            "session_id": str(session_id),
            "work_item_id": str(work_item_id),
            "branch": branch,
            "comment": verdict.comments,
            "server_key": str(payload["server_key"]),
            "repo_id": payload.get("repo_id"),
            "repo": store_key,
            "viewer_principal_id": str(persona.principal_id),
            "review_round": review_round,
        },
    )
    return {"work_item_id": str(work_item_id), "verdict": "request_changes"}


async def handle_merge_order(payload: dict[str, Any]) -> dict[str, Any]:
    """One Lead recommendation per delegation batch (>=2 PRs): the order to merge them.

    Enqueued by the delegate endpoint after the batch's delegate jobs -- the single-loop
    worker runs it once every PR in the batch exists. Best-effort by design: a model
    failure posts a fallback note listing the PRs in branch order rather than failing
    the job."""
    tenant_id = uuid.UUID(payload["tenant_id"])
    workspace_id = uuid.UUID(payload["workspace_id"])
    session_id = uuid.UUID(payload["session_id"])
    store_key = str(payload["store"])
    branches: list[str] = [str(b) for b in payload["branches"]]
    if len(branches) < 2:
        return {"skipped": "fewer than two PRs"}

    persona, profile = await _session_supervisor(tenant_id, session_id)
    store = GitStore(default_git_root())
    base = await _base_branch(tenant_id, payload.get("repo_id"), payload.get("base_branch"))

    known: list[dict[str, Any]] = []
    lines: list[str] = []
    for branch in branches:
        pr = await store.get_pr(store_key, branch) or {}
        stat: dict[str, int] = {}
        with contextlib.suppress(GitStoreError):
            stat = await store.diff_stat(store_key, branch, base=base)
        files = ""
        with contextlib.suppress(GitStoreError):
            listing = await store.diff_text(store_key, branch, base=base, max_chars=2000)
            files = ", ".join(
                ln.removeprefix("+++ b/") for ln in listing.splitlines() if ln.startswith("+++ b/")
            )
        ref = str(pr.get("pr_ref") or branch)
        title = str(pr.get("title") or branch)
        known.append({"ref": ref, "title": title, "html_url": pr.get("html_url")})
        lines.append(
            f"- {ref} — {title}: {stat.get('files', '?')} file(s) "
            f"(+{stat.get('insertions', '?')} −{stat.get('deletions', '?')}); "
            f"touches: {files or 'unknown'}"
        )

    model_string = f"{profile.provider}/{profile.model}"
    req = GenerationRequest(
        egress_policy=await load_egress_policy(tenant_id),
        model=model_string,
        messages=[
            {"role": "system", "content": f"{persona.persona_md}\n\n{_MERGE_ORDER_SYSTEM}"},
            {"role": "user", "content": "Pull requests in this batch:\n" + "\n".join(lines)},
        ],
        purpose=_PURPOSE,
        max_tokens=500,
        api_base=profile.api_base,
        params=dict(profile.params or {}),
        api_key=await resolve_connection_api_key(
            tenant_id, profile.credential_ref, encryptor=get_encryptor()
        ),
    )
    provider = get_model_provider(profile.provider)
    start = time.monotonic()
    ordered: list[dict[str, Any]]
    rationale: str
    try:
        plan = await provider.generate_structured(req, MergePlan)
        # Map the model's refs back onto known PRs by digits; anything it forgot is
        # appended in branch order, so the note always lists the whole batch.
        by_digits = {"".join(ch for ch in k["ref"] if ch.isdigit()): k for k in known}
        ordered, seen = [], set()
        for ref in plan.order:
            digits = "".join(ch for ch in str(ref) if ch.isdigit())
            hit = by_digits.get(digits)
            if hit is not None and hit["ref"] not in seen:
                ordered.append(hit)
                seen.add(hit["ref"])
        ordered.extend(k for k in known if k["ref"] not in seen)
        rationale = plan.rationale.strip()
        async with tenant_scope(tenant_id) as session:
            session.add(
                UsageRecordRow(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    persona_id=persona.id,
                    agent_id=profile.id,
                    provider=profile.provider,
                    model=profile.model,
                    purpose=_PURPOSE,
                    prompt_tokens=sum(
                        provider.count_tokens(str(m.get("content") or ""), model_string)
                        for m in req.messages
                    ),
                    completion_tokens=provider.count_tokens(rationale, model_string),
                    latency_ms=int((time.monotonic() - start) * 1000),
                )
            )
    except Exception as exc:  # noqa: BLE001 -- fallback note beats a failed job
        log.warning("merge_order.model_failed", error=str(exc))
        ordered = known
        rationale = "(automatic ordering unavailable — listed in branch order)"

    numbered = "\n".join(
        f"{i + 1}. **{k['ref']}** — {k['title']}"
        + (f" ({k['html_url']})" if k.get("html_url") else "")
        for i, k in enumerate(ordered)
    )
    await post_note(
        tenant_id,
        session_id,
        persona.principal_id,
        f"\U0001f9ed **Recommended merge order**\n\n{numbered}\n\n{rationale}",
    )
    return {"order": [k["ref"] for k in ordered]}
