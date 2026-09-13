"""Dev seed helper: create a tenant + owner principal + default workspace.

Not part of any runtime request path. Run directly (``python -m core.tenancy.seed``) or
imported by test fixtures that need a real, RLS-consistent tenant to work against instead
of hand-rolled fixture rows.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import select

# Workspace.vocabulary_overlay_id FKs to vocabulary_overlay.id by string reference --
# SQLAlchemy only resolves that at mapper-configuration time, which requires
# VocabularyOverlayRow's module to have been imported by *someone* first. core.tenancy.
# models can't do this import itself (core.vocabulary.models imports Base FROM this
# module, so the reverse import would be circular) -- seed_dev_tenant is what virtually
# every test in this codebase calls to get a real Workspace row, so the guard lives here.
import core.vocabulary.models  # noqa: E402, F401
from core.assembler.visibility import seed_default_scopes
from core.tenancy.models import Membership, Principal, Tenant, Workspace
from core.tenancy.scope import tenant_scope, unscoped_session


async def seed_dev_tenant(
    slug: str = "dev",
    tenant_name: str = "Dev Tenant",
    owner_display_name: str = "Dev Owner",
    workspace_key: str = "default",
    workspace_name: str = "Default Workspace",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Idempotent: returns the existing tenant/owner/workspace ids if already seeded."""
    async with unscoped_session() as session:
        existing = await session.scalar(select(Tenant).where(Tenant.slug == slug))
        if existing is not None:
            tenant_id = existing.id
        else:
            tenant = Tenant(slug=slug, name=tenant_name)
            session.add(tenant)
            await session.flush()
            tenant_id = tenant.id

    async with tenant_scope(tenant_id) as session:
        owner = await session.scalar(
            select(Principal).where(Principal.tenant_id == tenant_id, Principal.kind == "human")
        )
        if owner is None:
            owner = Principal(tenant_id=tenant_id, kind="human", display_name=owner_display_name)
            session.add(owner)
            await session.flush()
            session.add(Membership(tenant_id=tenant_id, principal_id=owner.id, role="owner"))

        workspace = await session.scalar(
            select(Workspace).where(
                Workspace.tenant_id == tenant_id, Workspace.key == workspace_key
            )
        )
        if workspace is None:
            workspace = Workspace(tenant_id=tenant_id, key=workspace_key, name=workspace_name)
            session.add(workspace)
            await session.flush()

        owner_id, workspace_id = owner.id, workspace.id

    # Outside the transaction above (which must commit first -- seed_default_scopes opens
    # its own tenant_scope() on a separate connection, whose FK to workspace.id would fail
    # to see an uncommitted row from a still-open transaction).
    await seed_default_scopes(tenant_id, workspace_id)
    return tenant_id, owner_id, workspace_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", default="dev")
    args = parser.parse_args()

    tenant_id, owner_id, workspace_id = asyncio.run(seed_dev_tenant(slug=args.slug))
    print(f"tenant={tenant_id} owner_principal={owner_id} workspace={workspace_id}")


if __name__ == "__main__":
    main()
