"""Where a configurable value actually comes from.

One rule decides what lives here: **if two tenants might reasonably want different
values, it is not an environment variable.** An environment variable is a deployment
fact -- where the database is, what this host can reach -- or the system default at the
bottom of this chain, which exists so a deployment works before any tenant has an
opinion.

    workspace.settings  ->  tenant.settings  ->  system default

The same shape the vocabulary overlay already uses (``core/vocabulary/service.py``,
plan §12.2), and the same shape D14's egress policy reads out of ``tenant.settings``.
Resolution lives here once so no call site re-derives the chain and quietly disagrees
with another about which layer wins.

**Absent means inherit; present means chosen.** A key that is not in a settings dict
falls through to the next layer; a key that is there is used exactly as stored, including
``0``, ``false`` and ``""``. Writing "inherit" is therefore *removing* the key, not
storing an empty value -- otherwise a workspace could never deliberately choose zero, and
an operator could never tell "not set" from "set to nothing".

Not everything configurable belongs here. The retrieval models (embedding, reranker) are
deployment-level on purpose and live in ``core/deployment_settings.py``: every tenant's
vectors sit in one column of one width, and the providers hold a loaded model in memory,
so picking those per tenant would be incoherent rather than merely expensive.
"""

from __future__ import annotations

import uuid
from typing import Any, TypeVar

from sqlalchemy import select

from core.tenancy.models import Tenant, Workspace
from core.tenancy.scope import tenant_scope

T = TypeVar("T")

_UNSET = object()


async def resolved_setting(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    key: str,
    default: T,
) -> T | Any:
    """The effective value of ``key`` for this workspace, or the tenant's, or ``default``.

    ``workspace_id`` is optional because some callers (a tenant-wide job, an admin view)
    legitimately have no workspace in hand -- they simply start one layer down rather
    than inventing a workspace to ask on behalf of.
    """
    layers = await _layers(tenant_id, workspace_id)
    for layer in layers:
        value = layer.get(key, _UNSET)
        if value is not _UNSET:
            return value
    return default


async def resolved_settings(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    defaults: dict[str, Any],
) -> dict[str, Any]:
    """Resolve several keys in one pass -- one round trip instead of one per key, which
    matters on the turn path where a handful of these are read together."""
    layers = await _layers(tenant_id, workspace_id)
    effective: dict[str, Any] = {}
    for key, default in defaults.items():
        effective[key] = default
        for layer in layers:
            value = layer.get(key, _UNSET)
            if value is not _UNSET:
                effective[key] = value
                break
    return effective


async def source_of(tenant_id: uuid.UUID, workspace_id: uuid.UUID | None, key: str) -> str:
    """Which layer supplies ``key`` -- "workspace", "tenant" or "default".

    For the UI: an inherited value should render as the thing it inherited, not as blank,
    so nobody mistakes "not set here" for "set to nothing".
    """
    layers = await _layers(tenant_id, workspace_id)
    names = ["workspace", "tenant"] if workspace_id is not None else ["tenant"]
    for name, layer in zip(names, layers, strict=True):
        if key in layer:
            return name
    return "default"


async def _layers(tenant_id: uuid.UUID, workspace_id: uuid.UUID | None) -> list[dict[str, Any]]:
    """Most specific first. One transaction, so a read cannot see a workspace from before
    a write and a tenant from after it."""
    async with tenant_scope(tenant_id) as session:
        layers: list[dict[str, Any]] = []
        if workspace_id is not None:
            workspace_settings = await session.scalar(
                select(Workspace.settings).where(Workspace.id == workspace_id)
            )
            layers.append(dict(workspace_settings or {}))
        tenant_settings = await session.scalar(
            select(Tenant.settings).where(Tenant.id == tenant_id)
        )
        layers.append(dict(tenant_settings or {}))
        return layers
