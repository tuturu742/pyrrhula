"""Superuser purge CLI -- the ONLY sanctioned hard delete in the system.

The app role (and therefore every API path) is REVOKE'd from deleting append-only tables and
RLS-scoped to one tenant, so it can only ever *archive* (soft-delete). Truly reclaiming a
tenant's rows -- including its append-only history -- needs the admin role, which bypasses
RLS and holds the DELETE grant. This tool is that path, run out-of-band by an operator, never
reachable from a request.

    python -m core.tenancy.purge --tenant <slug> [--archived-only] [--yes]
    python -m core.tenancy.purge --slug-prefix <prefix> [--yes]

Without ``--yes`` it is a dry run: it prints what it *would* delete and changes nothing.
``--slug-prefix`` is the bulk path for clearing test-suite leftovers (isolation-*, sched-*,
pd-*, ...). The reserved library tenant is always skipped.

Run it inside the deployment -- ``kubectl -n pyrrhula exec deploy/pyrrhula-api --`` or
``podman exec pyrrhula_api_1`` -- where the admin DSN already is. ``docs/operations.md``
has that and the rest of the operator tasks, including why the admin console offers
deactivation instead of this.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.tenancy.scope import admin_purge_session, dispose_engine

# Order matters: archived personas go first (via their principals, which cascades persona ->
# session -> message/manifest/...), which frees connection agents for the unreferenced-only
# delete that follows. knowledge_source cascades its own versions/entries/attachments;
# process_definition is SET NULL from session; a connection agent is RESTRICT-referenced by
# persona, so it is only deleted where no persona references it any more.
_COUNT_ARCHIVED = {
    "archived personas": (
        "SELECT count(*) FROM persona WHERE tenant_id=:t AND archived_at IS NOT NULL"
    ),
    "archived sessions": (
        "SELECT count(*) FROM session WHERE tenant_id=:t AND archived_at IS NOT NULL"
    ),
    "archived knowledge sources": (
        "SELECT count(*) FROM knowledge_source WHERE tenant_id=:t AND archived_at IS NOT NULL"
    ),
    "archived process definitions": (
        "SELECT count(*) FROM process_definition WHERE tenant_id=:t AND archived_at IS NOT NULL"
    ),
    "archived connections": (
        "SELECT count(*) FROM agent WHERE tenant_id=:t AND archived_at IS NOT NULL"
    ),
}

_COUNT_TENANT = {
    "agents": "SELECT count(*) FROM agent WHERE tenant_id=:t",
    "sessions": "SELECT count(*) FROM session WHERE tenant_id=:t",
    "messages": "SELECT count(*) FROM message WHERE tenant_id=:t",
    "knowledge sources": "SELECT count(*) FROM knowledge_source WHERE tenant_id=:t",
    "principals": "SELECT count(*) FROM principal WHERE tenant_id=:t",
}


async def _counts(
    session: AsyncSession, tenant_id: uuid.UUID, queries: dict[str, str]
) -> dict[str, int]:  # noqa: ANN001
    out: dict[str, int] = {}
    for label, sql in queries.items():
        out[label] = int((await session.scalar(text(sql), {"t": tenant_id})) or 0)
    return out


async def _purge_archived(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    p = {"t": tenant_id}
    # archived personas -> delete their principals (cascades persona -> session -> ...)
    await session.execute(
        text(
            "DELETE FROM principal WHERE kind='agent' AND tenant_id=:t AND id IN "
            "(SELECT principal_id FROM persona WHERE tenant_id=:t AND archived_at IS NOT NULL)"
        ),
        p,
    )
    await session.execute(
        text("DELETE FROM session WHERE tenant_id=:t AND archived_at IS NOT NULL"), p
    )
    await session.execute(
        text("DELETE FROM knowledge_source WHERE tenant_id=:t AND archived_at IS NOT NULL"), p
    )
    await session.execute(
        text("DELETE FROM process_definition WHERE tenant_id=:t AND archived_at IS NOT NULL"), p
    )
    # A connection agent is RESTRICT-referenced by persona: only remove archived connections
    # nothing points at any more (some become unreferenced once archived personas are gone).
    await session.execute(
        text(
            "DELETE FROM agent WHERE tenant_id=:t AND archived_at IS NOT NULL "
            "AND id NOT IN (SELECT agent_id FROM persona WHERE tenant_id=:t)"
        ),
        p,
    )


async def _resolve_tenants(
    session: AsyncSession, args: argparse.Namespace
) -> list[tuple[uuid.UUID, str, str]]:  # noqa: ANN001
    if args.tenant:
        rows = (
            await session.execute(
                text("SELECT id, slug, name FROM tenant WHERE slug=:s AND is_library=false"),
                {"s": args.tenant},
            )
        ).all()
    else:
        rows = (
            await session.execute(
                text(
                    "SELECT id, slug, name FROM tenant WHERE slug LIKE :p AND is_library=false "
                    "ORDER BY slug"
                ),
                {"p": f"{args.slug_prefix}%"},
            )
        ).all()
    excepts = set(getattr(args, "exclude", None) or [])
    return [(r[0], r[1], r[2]) for r in rows if r[1] not in excepts]


async def _run(args: argparse.Namespace) -> int:
    async with admin_purge_session() as session:
        tenants = await _resolve_tenants(session, args)
        if not tenants:
            target = args.tenant or f"{args.slug_prefix}*"
            print(f"no matching tenant(s) for {target!r}.")
            return 1

        print(f"{'PURGE' if args.yes else 'DRY RUN'} -- {len(tenants)} tenant(s):")
        for tenant_id, slug, name in tenants:
            queries = _COUNT_ARCHIVED if args.archived_only else _COUNT_TENANT
            counts = await _counts(session, tenant_id, queries)
            summary = ", ".join(f"{v} {k}" for k, v in counts.items())
            scope = "archived rows" if args.archived_only else "ENTIRE tenant"
            print(f"  [{slug}] {name} -- {scope}: {summary}")

            if not args.yes:
                continue
            if args.archived_only:
                await _purge_archived(session, tenant_id)
            else:
                await session.execute(text("DELETE FROM tenant WHERE id=:t"), {"t": tenant_id})
        if args.yes:
            print("done.")
        else:
            print("\nnothing changed. re-run with --yes to execute.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tenant", help="purge this exact tenant slug")
    group.add_argument("--slug-prefix", help="purge every tenant whose slug starts with this")
    parser.add_argument(
        "--archived-only",
        action="store_true",
        help="delete only archived rows (agents/sessions/lore/profiles/definitions), "
        "keeping the tenant. Only valid with --tenant.",
    )
    parser.add_argument(
        "--except",
        dest="exclude",
        action="append",
        metavar="SLUG",
        help="keep this tenant slug (repeatable) -- e.g. --slug-prefix '' --except dev",
    )
    parser.add_argument("--yes", action="store_true", help="actually delete (default: dry run)")
    args = parser.parse_args()

    if args.archived_only and not args.tenant:
        parser.error("--archived-only requires --tenant")

    async def _amain() -> int:
        try:
            return await _run(args)
        finally:
            await dispose_engine()

    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
