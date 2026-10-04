"""The operator's runtime-image allowlist: where any image a delegation runs may come from.

Stored on ``deployment_setting`` because it is a deployment decision -- an enterprise that
only trusts its own registry decides that once, for every organization on the platform.
Empty (the default) means unrestricted, which is exactly what the platform did before the
setting existed; nothing changes until an operator chooses.
"""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from core.images.namespace import invalidate_cache, normalise_allowlist_entry
from core.tenancy.scope import unscoped_session

ALLOWLIST_KEY = "runtime_image_allowlist"
_MAX_ENTRIES = 100


async def get_runtime_image_allowlist() -> list[str]:
    try:
        async with unscoped_session() as session:
            value = await session.scalar(
                text("SELECT value FROM deployment_setting WHERE key = :k").bindparams(
                    k=ALLOWLIST_KEY
                )
            )
    except ProgrammingError:  # before migrations: no table yet, so no restriction
        return []
    if not isinstance(value, dict):
        return []
    entries = value.get("prefixes")
    return [str(e) for e in entries] if isinstance(entries, list) else []


async def set_runtime_image_allowlist(entries: list[str]) -> list[str]:
    """Validate and store. Raises ValueError naming the bad entry."""
    if len(entries) > _MAX_ENTRIES:
        raise ValueError(f"at most {_MAX_ENTRIES} allowlist entries")
    cleaned: list[str] = []
    for entry in entries:
        if not entry.strip():
            continue
        normalised = normalise_allowlist_entry(entry)
        if normalised not in cleaned:
            cleaned.append(normalised)
    async with unscoped_session() as session:
        await session.execute(
            text(
                "INSERT INTO deployment_setting (key, value) VALUES (:k, CAST(:v AS jsonb)) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
            ).bindparams(k=ALLOWLIST_KEY, v=json.dumps({"prefixes": cleaned}))
        )
    invalidate_cache()
    return cleaned


# Ceilings on how much of the operator's build infrastructure one organization may use.
# Builds run on machines the operator pays for; a tenant that could queue without limit
# could starve every other one.
BUILD_LIMITS_KEY = "image_builds"
DEFAULT_BUILD_LIMITS: dict[str, int] = {
    "max_concurrent_per_tenant": 1,
    "max_per_day_per_tenant": 10,
    "timeout_seconds": 3600,
}
_LIMIT_BOUNDS = {
    "max_concurrent_per_tenant": (1, 20),
    "max_per_day_per_tenant": (1, 500),
    "timeout_seconds": (300, 6 * 3600),
}


async def get_image_build_limits() -> dict[str, int]:
    try:
        async with unscoped_session() as session:
            value = await session.scalar(
                text("SELECT value FROM deployment_setting WHERE key = :k").bindparams(
                    k=BUILD_LIMITS_KEY
                )
            )
    except ProgrammingError:
        value = None
    out = dict(DEFAULT_BUILD_LIMITS)
    if isinstance(value, dict):
        for name, (low, high) in _LIMIT_BOUNDS.items():
            if isinstance(value.get(name), int):
                out[name] = max(low, min(high, int(value[name])))
    return out


async def set_image_build_limits(values: dict[str, int]) -> dict[str, int]:
    unknown = set(values) - set(_LIMIT_BOUNDS)
    if unknown:
        raise ValueError(f"unknown limit(s): {sorted(unknown)}")
    merged = await get_image_build_limits()
    for name, raw in values.items():
        low, high = _LIMIT_BOUNDS[name]
        if not isinstance(raw, int) or not low <= raw <= high:
            raise ValueError(f"{name}: an integer from {low} to {high}")
        merged[name] = raw
    async with unscoped_session() as session:
        await session.execute(
            text(
                "INSERT INTO deployment_setting (key, value) VALUES (:k, CAST(:v AS jsonb)) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
            ).bindparams(k=BUILD_LIMITS_KEY, v=json.dumps(merged))
        )
    return merged
