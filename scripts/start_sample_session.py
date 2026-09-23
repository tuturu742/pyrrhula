"""Start a session in a seeded sample tenant, resolved by name rather than by id.

Every purge gives every tenant, workspace, persona and flow a new id, so a launcher that
hardcodes them is a launcher that works once. This resolves each one the way a person
does -- tenant by slug, flow by key, supervisor and participants by persona name or
persona_type -- so the same command starts the same session on a deployment rebuilt an
hour ago.

Mirrors ``api.routes.sessions.create_session_endpoint``: create, set the roster and the
agenda, bind the repositories (registering their git MCP server, which is what makes
`delegate_work_item` available to a phase that asks for it), name it, start the process
definition, and enqueue the first advance.

Usage::

    python scripts/start_sample_session.py --tenant loxia \\
        --flow investigate_plan_implement_review_merge \\
        --supervisor Architect --participants "Staff Dev,Senior Dev,Middle Dev,Junior Dev,QA" \\
        --repos loxia --name "Loxia docs" --agenda-file agenda.txt
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import uuid

GIT_TOOLS = ["delegate_work_item", "get_branch", "get_pull_request"]


async def _resolve(
    slug: str,
    flow_key: str,
    supervisor: str | None,
    participants: list[str],
    repo_keys: list[str],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, list[uuid.UUID], uuid.UUID, list[uuid.UUID]]:
    from sqlalchemy import select

    import core.agents.models  # noqa: F401  -- registers `persona` for the session FK
    import core.repos.models  # noqa: F401
    from core.agents.models import Persona
    from core.process.models import ProcessDefinitionRow
    from core.repos.models import RepoRow
    from core.tenancy.models import Tenant, Workspace
    from core.tenancy.scope import tenant_scope, unscoped_session

    async with unscoped_session() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == slug))
    if tenant_id is None:
        raise SystemExit(f"no tenant with slug {slug!r}")

    async with tenant_scope(tenant_id) as session:
        workspace_id = await session.scalar(
            select(Workspace.id)
            .where(Workspace.tenant_id == tenant_id)
            .order_by(Workspace.created_at)
        )
        personas = list(
            (await session.execute(select(Persona).where(Persona.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        # Newest active version of the flow: a reload publishes a new version, and
        # starting on a superseded one is how a fix that shipped never reaches a session.
        definition_id = await session.scalar(
            select(ProcessDefinitionRow.id)
            .where(
                ProcessDefinitionRow.tenant_id == tenant_id,
                ProcessDefinitionRow.key == flow_key,
                ProcessDefinitionRow.archived_at.is_(None),
            )
            .order_by(ProcessDefinitionRow.version.desc())
            .limit(1)
        )
        repo_ids = [
            r.id
            for r in (
                await session.execute(
                    select(RepoRow).where(
                        RepoRow.tenant_id == tenant_id, RepoRow.archived_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
            if not repo_keys or r.key in repo_keys
        ]

    if definition_id is None:
        raise SystemExit(f"no active flow {flow_key!r} in {slug!r}")

    by_name = {p.name: p for p in personas}
    if supervisor:
        if supervisor not in by_name:
            raise SystemExit(f"no persona named {supervisor!r}; have {sorted(by_name)}")
        supervisor_id = by_name[supervisor].id
    else:
        supervisors = [p for p in personas if p.persona_type == "supervisor"]
        if len(supervisors) != 1:
            raise SystemExit(
                "give --supervisor: "
                f"{len(supervisors)} personas have persona_type 'supervisor' in {slug!r}"
            )
        supervisor_id = supervisors[0].id

    if participants:
        missing = [n for n in participants if n not in by_name]
        if missing:
            raise SystemExit(f"no persona named {missing}; have {sorted(by_name)}")
        participant_ids = [by_name[n].id for n in participants]
    else:
        participant_ids = [
            p.id
            for p in personas
            if p.id != supervisor_id and p.persona_type in ("participant", "observer")
        ]

    missing_repos = sorted(set(repo_keys) - {r for r in repo_keys if repo_ids})
    if repo_keys and not repo_ids:
        raise SystemExit(f"no repositories matching {missing_repos or repo_keys} in {slug!r}")

    return tenant_id, workspace_id, supervisor_id, participant_ids, definition_id, repo_ids


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, help="tenant slug")
    parser.add_argument("--flow", required=True, help="process definition key")
    parser.add_argument("--supervisor", help="persona name; inferred when exactly one fits")
    parser.add_argument("--participants", default="", help="comma-separated persona names")
    parser.add_argument("--repos", default="", help="comma-separated repo keys; all if omitted")
    parser.add_argument("--name", default="", help="session name")
    parser.add_argument("--agenda", default="", help="agenda text")
    parser.add_argument("--agenda-file", type=pathlib.Path, help="agenda text, from a file")
    args = parser.parse_args()

    participants = [s.strip() for s in args.participants.split(",") if s.strip()]
    repo_keys = [s.strip() for s in args.repos.split(",") if s.strip()]
    agenda = args.agenda_file.read_text() if args.agenda_file else args.agenda

    (
        tenant_id,
        workspace_id,
        supervisor_id,
        participant_ids,
        definition_id,
        repo_ids,
    ) = await _resolve(args.tenant, args.flow, args.supervisor, participants, repo_keys)

    from api.job_queue_factory import get_job_queue
    from core.mcp.registry import register_server
    from core.process.authoring import get_definition
    from core.process.dsl.validator import validate_raw
    from core.process.interpreter import start_session as interpreter_start_session
    from core.process.skeleton import create_session
    from core.repos.service import set_session_repos, store_key
    from core.sessions.lifecycle import rename_session, set_session_agenda, set_session_roster

    sess = await create_session(tenant_id, workspace_id, supervisor_id)
    print("SESSION_ID", sess.id, flush=True)
    await set_session_roster(tenant_id, sess.id, supervisor_id, participant_ids)
    if agenda.strip():
        await set_session_agenda(tenant_id, sess.id, agenda)
    selected = await set_session_repos(tenant_id, sess.id, repo_ids)
    for repo in selected:
        # Binding a repository to a session is not the same as exposing its tools: the
        # git server is per repo and per workspace, and a phase that allows
        # `delegate_work_item` with no server registered simply never sees the tool.
        await register_server(
            tenant_id,
            workspace_id,
            f"git-{repo.key}",
            store_key(tenant_id, repo.key),
            enabled_tools=GIT_TOOLS,
            effectful_tools=["delegate_work_item"],
            require_confirmation=False,
        )
        print(f"repo {repo.key} bound", flush=True)
    if args.name:
        await rename_session(tenant_id, sess.id, args.name)

    row = await get_definition(tenant_id, definition_id)
    dsl, issues = validate_raw(row.definition)
    if dsl is None or issues:
        raise SystemExit(f"flow {args.flow!r} v{row.version} does not validate: {issues}")
    await interpreter_start_session(tenant_id, sess.id, dsl, row.id, row.version)
    print(f"started on {args.flow} v{row.version}", flush=True)

    job = await get_job_queue().enqueue(
        tenant_id, "advance_session", {"tenant_id": str(tenant_id), "session_id": str(sess.id)}
    )
    print(f"ADVANCE_QUEUED {job}", flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))  # type: ignore[func-returns-value]
