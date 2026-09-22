"""Pull-request state sync: what the host did to a pull request drives its work item.

A work item reached ``in_review`` when its pull request opened and then depended on
something inside Pyrrhula to move it again. Anything that happened on the host did not
come back. Merge somewhere else and the item stayed in review; close a pull request
without merging -- superseded, duplicated, or simply wrong -- and the item stayed in
review forever, because until the ``abandoned`` state existed there was nowhere else for
it to go. Eleven collected that way in one workspace, and once sessions could see the
workspace's entities they read as work in flight: a lead planning a fresh batch believed
five items were already being built and planned around them.

**Unknown is not closed.** ``pull_request_status`` returns ``None`` for a URL that does
not parse, a provider with no pull-request API, a failed request, a revoked token. Every
one of those means "cannot tell", and this sweep does nothing on any of them. Treating a
network error as a closed pull request would abandon live work.

Runs on the worker's idle tick, per tenant through ``tenant_scope`` -- the same shape and
the same RLS reason as ``worker.timeouts``. Transitions are idempotent by key, so a
second sweeper, or a restarted one, drives each item once.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import select

from adapters.gitremote.registry import resolve_remote
from adapters.mcp.git_store import GitStore, default_git_root
from api.encryptor_factory import get_encryptor
from api.permission_service_factory import get_permission_service
from core.agents.authoring import resolve_connection_api_key
from core.agents.models import Persona
from core.entities.mutation import transition
from core.entities.storage import get_entity
from core.repos.service import list_repos, store_key
from core.tenancy.models import Tenant
from core.tenancy.scope import tenant_scope, unscoped_session

log = structlog.get_logger()

# Lifecycle states a sync may still move. Anything else has already finished, and a
# finished item is not re-decided because a host changed its mind about an old branch.
_LIVE_STATES = frozenset({"in_progress", "in_review", "changes_requested", "approved"})

# What the host's verdict means for the work item.
_TRIGGER_FOR = {"merged": "merge", "closed": "abandon"}

# `merge` runs from `approved` only; a pull request merged outside Pyrrhula may never
# have been approved inside it, so the item is walked up first. `abandon` is reachable
# from anywhere and needs no such path.
_PATH_TO_MERGE = {
    "in_progress": ("submit_for_review", "approve", "merge"),
    "in_review": ("approve", "merge"),
    "changes_requested": ("rework", "submit_for_review", "approve", "merge"),
    "approved": ("merge",),
}


def _pr_number(record: dict[str, Any]) -> int | None:
    raw = record.get("pr_ref") or record.get("number")
    if raw is None:
        return None
    text = str(raw).lstrip("#").strip()
    try:
        return int(text)
    except ValueError:
        return None


async def _drive(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    work_item_id: uuid.UUID,
    state: str,
    current: str,
    branch: str,
) -> bool:
    """Walk the item to where the host says it is. Returns whether anything moved."""
    triggers = _PATH_TO_MERGE.get(current, ()) if state == "merged" else (_TRIGGER_FOR[state],)
    if not triggers:
        return False
    for trigger in triggers:
        await transition(
            principal_id,
            tenant_id,
            workspace_id,
            work_item_id,
            "lifecycle",
            trigger,
            # Keyed on the outcome being applied, not on the moment of applying it: a
            # sweep that runs every minute must not re-drive what it already drove.
            idempotency_key=f"pr-sync:{work_item_id}:{branch}:{state}:{trigger}",
            permission_service=get_permission_service(),
            cause="system",
        )
    return True


async def _workspace_supervisor_principal(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> uuid.UUID | None:
    """Who the sync acts as. The workspace's supervisor persona: it is the role that
    already owns the work-item lifecycle everywhere else (dispatch, review, merge), so
    the audit trail reads the same whether a transition came from a session turn or from
    a host's verdict arriving late. No supervisor, no transition -- the sweep does not
    invent an authority to act under.
    """
    async with tenant_scope(tenant_id) as session:
        return await session.scalar(
            select(Persona.principal_id)
            .where(
                Persona.tenant_id == tenant_id,
                Persona.workspace_id == workspace_id,
                Persona.persona_type == "supervisor",
            )
            .order_by(Persona.created_at)
            .limit(1)
        )


async def sync_pull_requests_for_tenant(tenant_id: uuid.UUID) -> int:
    """One tenant's worth of the sync. Split out because syncing a single tenant is a
    real operation on its own -- after fixing a token, say, or before a demo."""
    store = GitStore(default_git_root())
    encryptor = get_encryptor()
    moved = 0

    for repo in await list_repos(tenant_id):
        source_url = (repo.source_url or "").strip()
        if not source_url or repo.credential_ref is None:
            continue  # store-only repo: there is no host to ask.
        remote = resolve_remote(source_url, repo.provider)
        if remote is None:
            continue  # nothing that speaks a pull-request API
        try:
            token = await resolve_connection_api_key(
                tenant_id, str(repo.credential_ref), encryptor=encryptor
            )
        except Exception as exc:  # noqa: BLE001 -- one unreadable credential, not a sweep
            log.warning("pr_sync.credential_failed", repo=repo.key, error=str(exc)[:200])
            continue

        try:
            records = await store.list_prs(store_key(tenant_id, repo.key))
        except Exception as exc:  # noqa: BLE001 -- a repo with no store yet
            log.warning("pr_sync.store_failed", repo=repo.key, error=str(exc)[:200])
            continue

        for record in records:
            raw_id = record.get("work_item_id")
            number = _pr_number(record)
            if not raw_id or number is None:
                continue  # recorded before the link existed, or never opened remotely
            try:
                work_item_id = uuid.UUID(str(raw_id))
            except ValueError:
                continue
            entity = await get_entity(tenant_id, work_item_id)
            if entity is None:
                continue
            current = str(entity.fsm_states.get("lifecycle") or "")
            if current not in _LIVE_STATES:
                continue

            status = await remote.pull_request_status(source_url, token, number=number)
            if status is None or status.state == "open":
                continue  # unknown, or nothing to do -- both are "leave it alone"

            branch = str(record.get("branch") or "")
            actor = await _workspace_supervisor_principal(tenant_id, entity.workspace_id)
            if actor is None:
                log.warning(
                    "pr_sync.no_actor",
                    workspace_id=str(entity.workspace_id),
                    work_item_id=str(work_item_id),
                )
                continue
            try:
                changed = await _drive(
                    tenant_id,
                    entity.workspace_id,
                    actor,
                    work_item_id,
                    status.state,
                    current,
                    branch,
                )
            except Exception as exc:  # noqa: BLE001 -- one item must not stop the sweep
                log.warning(
                    "pr_sync.transition_failed",
                    work_item_id=str(work_item_id),
                    state=status.state,
                    error=str(exc)[:200],
                )
                continue
            if changed:
                moved += 1
                log.info(
                    "pr_sync.work_item_followed_its_pull_request",
                    work_item_id=str(work_item_id),
                    repo=repo.key,
                    pr=f"#{number}",
                    from_state=current,
                    host_state=status.state,
                )
    return moved


async def sync_pull_requests() -> int:
    """One sweep tick across every tenant. Returns how many work items moved."""
    async with unscoped_session() as session:
        tenant_ids = list((await session.execute(select(Tenant.id))).scalars())
    return sum([await sync_pull_requests_for_tenant(t) for t in tenant_ids])
