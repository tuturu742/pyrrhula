"""Organization preferences: tenant settings that used to be environment variables.

The rule that moved them (``docs/configuration.md``): if two organizations might
reasonably want different values, it is not an environment variable. How long a login
lasts, how long a preview serves by default, and whether retrieval pays for a reranking
pass are all that kind of choice -- an enterprise wants eight-hour sessions and a hobby
box wants thirty days; a workspace of long demos wants long previews; a tenant on a
small box may prefer WRRF order to a cross-encoder's latency.

Each resolves through the settings chain with a built-in default underneath
(``core.settings.resolve``), so an organization that has never opened the page behaves
exactly as before. Writes go to ``tenant.settings``; a removed key inherits the default,
the same convention every other setting on the chain uses.

**Reads fail closed to the default.** A hand-edited or stale value that does not parse,
or a preview lifetime above the operator's ceiling, reads as the default rather than as
"no limit" -- for the same reason ``generation_limits`` does: the defaults exist to end
an unbounded state, so a bad value must not reopen one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select

from core.settings.resolve import resolved_settings
from core.tenancy.models import Tenant
from core.tenancy.scope import tenant_scope

SESSION_LIFETIME_KEY = "session_lifetime_seconds"
PREVIEW_TTL_KEY = "preview_ttl_seconds"
RERANKER_KEY = "reranker_enabled"

DEFAULT_SESSION_LIFETIME_SECONDS = 24 * 3600
# Five minutes is the shortest login that is still a login rather than a one-shot token;
# thirty days is where "remember me" ends and a credential that never expires begins.
MIN_SESSION_LIFETIME_SECONDS = 5 * 60
MAX_SESSION_LIFETIME_SECONDS = 30 * 24 * 3600

# Previews hold a container for their whole life, so the default is a working session,
# not a week. The ceiling is the operator's (``PYRRHULA_PREVIEW_MAX_TTL_SECONDS``).
DEFAULT_PREVIEW_TTL_SECONDS = 4 * 3600
MIN_PREVIEW_TTL_SECONDS = 60


@dataclass(frozen=True)
class TenantPreferences:
    session_lifetime_seconds: int = DEFAULT_SESSION_LIFETIME_SECONDS
    preview_ttl_seconds: int = DEFAULT_PREVIEW_TTL_SECONDS
    reranker_enabled: bool = True


def preview_ttl_ceiling() -> int:
    from core.config import get_settings

    return int(get_settings().preview_max_ttl_seconds)


def _coerce(raw: dict[str, Any]) -> TenantPreferences:
    """Stored values, each falling back to its default when unusable."""
    prefs = TenantPreferences()
    try:
        lifetime = int(raw.get(SESSION_LIFETIME_KEY, prefs.session_lifetime_seconds))
    except (TypeError, ValueError):
        lifetime = prefs.session_lifetime_seconds
    if not MIN_SESSION_LIFETIME_SECONDS <= lifetime <= MAX_SESSION_LIFETIME_SECONDS:
        lifetime = prefs.session_lifetime_seconds

    try:
        ttl = int(raw.get(PREVIEW_TTL_KEY, prefs.preview_ttl_seconds))
    except (TypeError, ValueError):
        ttl = prefs.preview_ttl_seconds
    if not MIN_PREVIEW_TTL_SECONDS <= ttl <= preview_ttl_ceiling():
        ttl = min(prefs.preview_ttl_seconds, preview_ttl_ceiling())

    reranker = raw.get(RERANKER_KEY, prefs.reranker_enabled)
    if not isinstance(reranker, bool):
        reranker = prefs.reranker_enabled
    return replace(
        prefs,
        session_lifetime_seconds=lifetime,
        preview_ttl_seconds=ttl,
        reranker_enabled=reranker,
    )


async def get_preferences(tenant_id: uuid.UUID) -> TenantPreferences:
    """The organization's effective preferences, defaults filled in."""
    defaults = TenantPreferences()
    effective = await resolved_settings(
        tenant_id,
        None,
        {
            SESSION_LIFETIME_KEY: defaults.session_lifetime_seconds,
            PREVIEW_TTL_KEY: defaults.preview_ttl_seconds,
            RERANKER_KEY: defaults.reranker_enabled,
        },
    )
    return _coerce(effective)


async def session_lifetime_seconds(tenant_id: uuid.UUID) -> int:
    """How long a login issued for this organization lasts."""
    return (await get_preferences(tenant_id)).session_lifetime_seconds


async def preview_ttl_seconds(tenant_id: uuid.UUID) -> int:
    """The default lifetime of a preview this organization starts."""
    return (await get_preferences(tenant_id)).preview_ttl_seconds


async def reranker_wanted(tenant_id: uuid.UUID) -> bool:
    """Whether this organization's retrieval should use the deployment's reranker."""
    return (await get_preferences(tenant_id)).reranker_enabled


def validate(values: dict[str, Any]) -> dict[str, Any]:
    """Only the keys present are checked; an absent key keeps its current value.

    Raises ``ValueError`` with a message the API can return as-is."""
    out: dict[str, Any] = {}
    if SESSION_LIFETIME_KEY in values:
        try:
            lifetime = int(values[SESSION_LIFETIME_KEY])
        except (TypeError, ValueError) as exc:
            raise ValueError("session_lifetime_seconds must be a whole number of seconds") from exc
        if not MIN_SESSION_LIFETIME_SECONDS <= lifetime <= MAX_SESSION_LIFETIME_SECONDS:
            raise ValueError(
                "session_lifetime_seconds must be between "
                f"{MIN_SESSION_LIFETIME_SECONDS} and {MAX_SESSION_LIFETIME_SECONDS}"
            )
        out[SESSION_LIFETIME_KEY] = lifetime
    if PREVIEW_TTL_KEY in values:
        try:
            ttl = int(values[PREVIEW_TTL_KEY])
        except (TypeError, ValueError) as exc:
            raise ValueError("preview_ttl_seconds must be a whole number of seconds") from exc
        ceiling = preview_ttl_ceiling()
        if not MIN_PREVIEW_TTL_SECONDS <= ttl <= ceiling:
            raise ValueError(
                f"preview_ttl_seconds must be between {MIN_PREVIEW_TTL_SECONDS} and "
                f"{ceiling} (this deployment's ceiling)"
            )
        out[PREVIEW_TTL_KEY] = ttl
    if RERANKER_KEY in values:
        if not isinstance(values[RERANKER_KEY], bool):
            raise ValueError("reranker_enabled must be true or false")
        out[RERANKER_KEY] = values[RERANKER_KEY]
    return out


async def set_preferences(tenant_id: uuid.UUID, values: dict[str, Any]) -> TenantPreferences:
    """Store the given keys on the tenant; the rest are left as they were."""
    validated = validate(values)
    async with tenant_scope(tenant_id) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        if tenant is None:
            raise ValueError("no such tenant")
        tenant.settings = {**(tenant.settings or {}), **validated}
    return await get_preferences(tenant_id)
