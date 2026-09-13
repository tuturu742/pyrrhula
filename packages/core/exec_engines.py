"""Exec-engine declarations + per-tenant selection.

The OPERATOR declares which container engines this deployment offers
(``PYRRHULA_EXEC_ENGINES``, a JSON list — see docs/exec-engines.md); each entry has a
stable ``key``, a ``kind`` the factory knows how to build (``socket`` today,
``kubernetes`` next, cloud kinds later), and kind-specific fields. TENANTS then pick
one of the declared keys (``tenant.settings["exec_engine"]``) — e.g. a local socket
pool vs a shared cluster. First declared entry is the default.

Back-compat: with no declaration but the legacy ``PYRRHULA_EXEC_SOCKET`` set, a single
``{key: "local", kind: "socket"}`` engine is synthesized, so existing deployments keep
working with zero config changes.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from sqlalchemy import select

from core.config import get_settings
from core.tenancy.models import Tenant
from core.tenancy.scope import tenant_scope


class UnknownEngineError(Exception):
    pass


def declared_engines() -> list[dict[str, Any]]:
    raw = (get_settings().exec_engines or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise UnknownEngineError(f"PYRRHULA_EXEC_ENGINES is not valid JSON: {exc}") from exc
        engines = [e for e in parsed if isinstance(e, dict) and e.get("key") and e.get("kind")]
        if engines:
            return engines
    socket = os.environ.get("PYRRHULA_EXEC_SOCKET", "")
    if socket:
        return [
            {
                "key": "local",
                "kind": "socket",
                "socket": socket,
                "label": "Local containers",
            }
        ]
    return []


def default_engine_key() -> str | None:
    engines = declared_engines()
    return str(engines[0]["key"]) if engines else None


def engine_by_key(key: str | None) -> dict[str, Any] | None:
    engines = declared_engines()
    if not engines:
        return None
    for engine in engines:
        if engine["key"] == key:
            return engine
    return engines[0]  # unknown/None -> deployment default


async def get_tenant_engine_key(tenant_id: uuid.UUID) -> str | None:
    async with tenant_scope(tenant_id) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        chosen = (tenant.settings or {}).get("exec_engine") if tenant else None
    if chosen and any(e["key"] == chosen for e in declared_engines()):
        return str(chosen)
    return default_engine_key()


async def set_tenant_engine_key(tenant_id: uuid.UUID, key: str) -> None:
    if not any(e["key"] == key for e in declared_engines()):
        raise UnknownEngineError(f"engine {key!r} is not declared by this deployment")
    async with tenant_scope(tenant_id) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        if tenant is None:
            raise UnknownEngineError("no such tenant")
        tenant.settings = {**(tenant.settings or {}), "exec_engine": key}
