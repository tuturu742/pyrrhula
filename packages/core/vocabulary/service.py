"""Vocabulary overlay resolution: list available overlays, resolve
the one effective for a workspace (fallback chain: workspace override -> tenant default
-> system default -> [frontend falls back to the key itself]), and switch either.
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select

from core.tenancy.models import Tenant, Workspace
from core.tenancy.scope import tenant_scope
from core.vocabulary.models import VocabularyOverlayRow

SYSTEM_DEFAULT_OVERLAY_KEY = "rpg_v1"


async def list_overlays(tenant_id: uuid.UUID) -> list[VocabularyOverlayRow]:
    """System overlays (visible to every tenant) plus this tenant's own custom ones (no
    authoring UI yet, so today this is always just the shipped system sets)."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(VocabularyOverlayRow)
                .where(
                    or_(
                        VocabularyOverlayRow.tenant_id.is_(None),
                        VocabularyOverlayRow.tenant_id == tenant_id,
                    )
                )
                .order_by(VocabularyOverlayRow.key)
            )
        ).scalars()
        return list(rows)


async def get_overlay_by_key(tenant_id: uuid.UUID, key: str) -> VocabularyOverlayRow | None:
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(VocabularyOverlayRow).where(
                VocabularyOverlayRow.key == key,
                or_(
                    VocabularyOverlayRow.tenant_id.is_(None),
                    VocabularyOverlayRow.tenant_id == tenant_id,
                ),
            )
        )
        return row


async def resolve_overlay_for_workspace(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> VocabularyOverlayRow | None:
    """The fallback chain: the workspace's own override, else the tenant's default (a
    key stored in ``tenant.settings``), else the shipped system default. Returns
    ``None`` only if even the system default overlay is somehow missing (a genuine
    misconfiguration) -- the frontend's own key-itself fallback is the last resort for
    that case, not something this function needs to paper over."""
    async with tenant_scope(tenant_id) as session:
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None:
            return None
        if workspace.vocabulary_overlay_id is not None:
            overlay = await session.get(VocabularyOverlayRow, workspace.vocabulary_overlay_id)
            if overlay is not None:
                return overlay

        tenant = await session.get(Tenant, tenant_id)
        default_key = (
            tenant.settings.get("default_vocabulary_overlay_key") if tenant is not None else None
        )

    if isinstance(default_key, str):
        overlay = await get_overlay_by_key(tenant_id, default_key)
        if overlay is not None:
            return overlay

    return await get_overlay_by_key(tenant_id, SYSTEM_DEFAULT_OVERLAY_KEY)


async def upsert_overlay(
    tenant_id: uuid.UUID, key: str, name: str, labels: dict[str, str]
) -> VocabularyOverlayRow:
    """Create or replace a **tenant** overlay (never a system one -- the RLS policy's
    WITH CHECK would refuse anyway). The moddable-workflow feature materializes a selected
    workflow's label_overrides through this (key ``wf-<workflow_key>``), so the existing
    resolve chain serves custom labels with no changes of its own."""
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(VocabularyOverlayRow).where(
                VocabularyOverlayRow.tenant_id == tenant_id,
                VocabularyOverlayRow.key == key,
            )
        )
        if row is None:
            row = VocabularyOverlayRow(tenant_id=tenant_id, key=key, name=name, labels=labels)
            session.add(row)
        else:
            row.name = name
            row.labels = dict(labels)
        await session.flush()
        await session.refresh(row)
        return row


async def set_workspace_overlay(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, overlay_id: uuid.UUID | None
) -> Workspace:
    async with tenant_scope(tenant_id) as session:
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None:
            raise ValueError(f"no workspace {workspace_id} in this tenant")
        workspace.vocabulary_overlay_id = overlay_id
        await session.flush()
        return workspace


async def set_tenant_default_overlay(tenant_id: uuid.UUID, overlay_key: str | None) -> Tenant:
    async with tenant_scope(tenant_id) as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise ValueError(f"no tenant {tenant_id}")
        settings = dict(tenant.settings)
        if overlay_key is None:
            settings.pop("default_vocabulary_overlay_key", None)
        else:
            settings["default_vocabulary_overlay_key"] = overlay_key
        tenant.settings = settings
        await session.flush()
        return tenant
