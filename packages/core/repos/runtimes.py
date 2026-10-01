"""Which images a tenant may build in.

The catalog used to be four entries in a module-level dict -- debian, node20, python312,
java21 -- and the only way past it was ``runtime: "custom"`` with a fully qualified image
typed into the repo row. That made "custom" the common case rather than the exception:
a Rust project, a Go project, or anything on an internal registry had no name in the
catalog, so each repo carried its own copy of an image reference nobody could see from
one place or update in one place.

A tenant registers its own now. The deployment's built-ins stay as the floor, and a
tenant entry with the same key wins -- which is the point as much as adding new ones is:
an air-gapped deployment overrides ``debian`` to name its internal mirror once, and every
repo that says ``debian`` follows, instead of each one hardcoding the mirror.

Stored on ``tenant.settings``, the same place ``exec_engine`` lives (``core.exec_engines``),
for the same reason: this is a handful of small values per tenant, and a table would add
RLS surface for no query it needs to answer.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from core.tenancy.models import Tenant
from core.tenancy.scope import tenant_scope

SETTING_KEY = "runtimes"

# The deployment's floor. Every tenant sees these unless it names one itself.
BUILTIN_RUNTIMES: dict[str, dict[str, object]] = {
    "debian": {
        "image": "docker.io/library/debian:bookworm",
        "setup": [
            "apt-get update && apt-get install -y --no-install-recommends git ca-certificates"
        ],
    },
    "node20": {"image": "docker.io/library/node:20-bookworm", "setup": []},
    "python312": {"image": "docker.io/library/python:3.12-bookworm", "setup": []},
    "java21": {
        "image": "docker.io/library/eclipse-temurin:21-jdk",
        "setup": [
            "apt-get update && apt-get install -y --no-install-recommends git ca-certificates"
        ],
    },
}

# "custom" is not a runtime, it is the absence of one: the repo names its own image and
# the catalog is not consulted. Reserved so a tenant cannot register a key that would
# silently stop meaning that.
CUSTOM = "custom"

_MAX_RUNTIMES = 50
_MAX_SETUP_CMDS = 20
_MAX_CMD_LEN = 511
_MAX_KEY_LEN = 63


class InvalidRuntimeError(ValueError):
    """A runtime registration that cannot be honoured. Always names the field."""


def validate_entry(key: str, image: str, setup: list[str]) -> dict[str, object]:
    """Check one registration and return it in storage shape."""
    key = (key or "").strip()
    if not key:
        raise InvalidRuntimeError("runtime key is required")
    if key == CUSTOM:
        raise InvalidRuntimeError(
            f"{CUSTOM!r} is reserved: it means the repo names its own image, "
            "so a runtime by that name could never be selected"
        )
    if len(key) > _MAX_KEY_LEN:
        raise InvalidRuntimeError(f"runtime key is longer than {_MAX_KEY_LEN} characters")
    if not key.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise InvalidRuntimeError("runtime key may contain letters, digits, '-', '_' and '.' only")

    from core.repos.image_ref import ImageRefError, normalise_image_ref

    try:
        normalised = normalise_image_ref(image)
    except ImageRefError as exc:
        raise InvalidRuntimeError(f"runtime {key!r}: {exc}") from exc
    if not normalised:
        raise InvalidRuntimeError(f"runtime {key!r}: image is required")
    image = normalised

    if len(setup) > _MAX_SETUP_CMDS:
        raise InvalidRuntimeError(f"runtime {key!r}: at most {_MAX_SETUP_CMDS} setup commands")
    cleaned: list[str] = []
    for cmd in setup:
        text = str(cmd).strip()
        if not text:
            continue
        if len(text) > _MAX_CMD_LEN:
            raise InvalidRuntimeError(
                f"runtime {key!r}: a setup command is longer than {_MAX_CMD_LEN} characters"
            )
        cleaned.append(text)
    return {"image": image, "setup": cleaned}


def _tenant_entries(settings: dict[str, Any] | None) -> dict[str, dict[str, object]]:
    raw = (settings or {}).get(SETTING_KEY)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, object]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict) or not value.get("image"):
            continue  # a malformed entry is ignored, never raised on a read path
        setup = value.get("setup")
        out[str(key)] = {
            "image": str(value["image"]),
            "setup": [str(c) for c in setup] if isinstance(setup, list) else [],
        }
    return out


async def resolved_runtimes(tenant_id: uuid.UUID) -> dict[str, dict[str, object]]:
    """Every runtime this tenant may select, built-ins first and its own on top."""
    async with tenant_scope(tenant_id) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        settings = tenant.settings if tenant else None
    return {**BUILTIN_RUNTIMES, **_tenant_entries(settings)}


async def get_runtime(tenant_id: uuid.UUID, key: str | None) -> dict[str, object] | None:
    if not key or key == CUSTOM:
        return None
    return (await resolved_runtimes(tenant_id)).get(key)


async def register_runtime(
    tenant_id: uuid.UUID, key: str, image: str, setup: list[str] | None = None
) -> dict[str, object]:
    """Add or replace one of this tenant's runtimes."""
    entry = validate_entry(key, image, list(setup or []))
    # Here in core, not only at the route: any path that registers a runtime must be held
    # to the same rule -- not another organization's image, and within the allowlist.
    from core.images.namespace import check_image_ref_for_tenant
    from core.repos.image_ref import ImageRefError

    try:
        await check_image_ref_for_tenant(tenant_id, str(entry["image"]))
    except ImageRefError as exc:
        raise InvalidRuntimeError(f"runtime {key!r}: {exc}") from exc
    async with tenant_scope(tenant_id) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        if tenant is None:
            raise InvalidRuntimeError("no such tenant")
        existing = _tenant_entries(tenant.settings)
        if key.strip() not in existing and len(existing) >= _MAX_RUNTIMES:
            raise InvalidRuntimeError(f"a tenant may register at most {_MAX_RUNTIMES} runtimes")
        tenant.settings = {
            **(tenant.settings or {}),
            SETTING_KEY: {**existing, key.strip(): entry},
        }
    return entry


async def remove_runtime(tenant_id: uuid.UUID, key: str) -> bool:
    """Forget one of this tenant's runtimes. Returns whether it had one by that name.

    A built-in of the same name becomes visible again rather than disappearing -- removing
    an override is how a tenant goes back to the deployment's image.
    """
    async with tenant_scope(tenant_id) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        if tenant is None:
            return False
        existing = _tenant_entries(tenant.settings)
        if key not in existing:
            return False
        remaining = {k: v for k, v in existing.items() if k != key}
        tenant.settings = {**(tenant.settings or {}), SETTING_KEY: remaining}
    return True
