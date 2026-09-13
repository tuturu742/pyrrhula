"""The superuser purge CLI: dry-run changes nothing, --yes truly removes (a whole tenant, or
just its archived rows). Runs against the real DB as the admin role, the one path allowed to
hard-delete past RLS and the append-only grants."""

from __future__ import annotations

import argparse
import uuid

from sqlalchemy import text

from api.encryptor_factory import get_encryptor
from core.agents.authoring import archive_persona, create_agent, create_persona
from core.tenancy import purge
from core.tenancy.provisioning import create_tenant
from core.tenancy.scope import admin_purge_session


def _args(**kw: object) -> argparse.Namespace:
    base = {"tenant": None, "slug_prefix": None, "archived_only": False, "yes": False}
    base.update(kw)
    return argparse.Namespace(**base)


async def _tenant_exists(slug: str) -> bool:
    async with admin_purge_session() as s:
        return bool(await s.scalar(text("SELECT count(*) FROM tenant WHERE slug=:s"), {"s": slug}))


async def test_dry_run_then_yes_purges_whole_tenant(db_available: None) -> None:
    slug = f"purge-{uuid.uuid4().hex[:8]}"
    await create_tenant("Purge Me", slug)
    assert await _tenant_exists(slug)

    # dry run leaves it untouched
    assert await purge._run(_args(tenant=slug)) == 0
    assert await _tenant_exists(slug)

    # --yes removes it
    assert await purge._run(_args(tenant=slug, yes=True)) == 0
    assert not await _tenant_exists(slug)


async def test_archived_only_removes_archived_agent_keeps_tenant(db_available: None) -> None:
    slug = f"purge-arch-{uuid.uuid4().hex[:8]}"
    tenant_id, workspace_id = await create_tenant("Keep Me", slug)
    profile = await create_agent(tenant_id, "p", "echo", "echo", encryptor=get_encryptor())
    live = await create_persona(tenant_id, workspace_id, "live", "Live", profile.id)
    doomed = await create_persona(tenant_id, workspace_id, "doomed", "Doomed", profile.id)
    await archive_persona(tenant_id, doomed.id)

    assert await purge._run(_args(tenant=slug, archived_only=True, yes=True)) == 0

    async with admin_purge_session() as s:
        remaining = {
            r[0]
            for r in (
                await s.execute(text("SELECT id FROM persona WHERE tenant_id=:t"), {"t": tenant_id})
            ).all()
        }
    assert live.id in remaining and doomed.id not in remaining
    # the tenant itself survives an archived-only purge
    assert await _tenant_exists(slug)

    # cleanup: remove the throwaway tenant entirely
    assert await purge._run(_args(tenant=slug, yes=True)) == 0
