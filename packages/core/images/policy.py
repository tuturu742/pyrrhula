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
