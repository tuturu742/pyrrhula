"""Which image references a tenant may use -- one check, applied at every door.

Two rules, and both are about references rather than about pulls, because the engine that
pulls cannot tell one tenant from another:

1. **Managed namespaces belong to their tenant.** Every declared registry has a path under
   which images built for a tenant live (``<host>/<prefix>/t<tenant hex>/…``). A reference
   inside *any* registry's managed area must be inside the *caller's own* part of it.
   Otherwise one tenant could run another's image just by typing its name.
2. **The operator's allowlist, when set.** A deployment can restrict where runtime images
   may come from at all. Empty means unrestricted -- today's behaviour, unchanged until an
   operator chooses otherwise. Images in the caller's own managed namespace always pass:
   the operator declared that registry.

References are compared in canonical form (``python:3`` and ``docker.io/library/python:3``
are the same image), and every spelling of a registry's host counts, or the policy would
have a bypass that is just another way of typing it. The full tenant UUID is used, not a
prefix of it: eight hex characters collide by the birthday bound at tens of thousands of
tenants, and a collision here would make another tenant's images "your own".

The pure functions take the registries and allowlist as arguments so they can be tested
without a database; ``check_image_ref_for_tenant`` loads them.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from core.repos.image_ref import ImageRefError, canonical

_HUB_ALIASES = frozenset({"docker.io", "index.docker.io", "registry-1.docker.io"})


def canon_host(host: str) -> str:
    host = host.strip().lower().rstrip("/")
    return "docker.io" if host in _HUB_ALIASES else host


@dataclass(frozen=True)
class RegistryNamespace:
    """The parts of a declared registry the namespace rules need."""

    key: str
    pull_host: str
    path_prefix: str
    path_style: str = "nested"  # nested | flat (Docker Hub allows no deeper paths)
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def hosts(self) -> list[str]:
        seen: list[str] = []
        for host in (self.pull_host, *self.aliases):
            canon = canon_host(host)
            if canon and canon not in seen:
                seen.append(canon)
        return seen


def tenant_hex(tenant_id: uuid.UUID) -> str:
    return tenant_id.hex


def managed_roots(registry: RegistryNamespace) -> list[str]:
    """Every canonical prefix under which this registry keeps tenant images."""
    prefix = registry.path_prefix.strip("/")
    tail = f"{prefix}/" if registry.path_style == "nested" else f"{prefix}/pyr-t"
    return [f"{host}/{tail}" for host in registry.hosts()]


def own_roots(registry: RegistryNamespace, tenant_id: uuid.UUID) -> list[str]:
    """The caller's own part of each managed root."""
    prefix = registry.path_prefix.strip("/")
    hexid = tenant_hex(tenant_id)
    tail = f"{prefix}/t{hexid}/" if registry.path_style == "nested" else f"{prefix}/pyr-t{hexid}-"
    return [f"{host}/{tail}" for host in registry.hosts()]


def repository_for(registry: RegistryNamespace, tenant_id: uuid.UUID, name: str) -> str:
    """Where an image named ``name`` for this tenant lives (no tag)."""
    prefix = registry.path_prefix.strip("/")
    hexid = tenant_hex(tenant_id)
    host = canon_host(registry.pull_host)
    if registry.path_style == "nested":
        return f"{host}/{prefix}/t{hexid}/{name}"
    return f"{host}/{prefix}/pyr-t{hexid}-{name}"


def normalise_allowlist_entry(entry: str) -> str:
    """``ghcr.io`` -> ``ghcr.io/``; ``index.docker.io/library/`` -> ``docker.io/library/``.

    An entry must start with a registry host: a bare ``python`` would be ambiguous between
    "the python image" and "anything starting with python".
    """
    entry = entry.strip()
    if not entry:
        raise ValueError("an allowlist entry cannot be empty")
    host, _, rest = entry.partition("/")
    if not ("." in host or ":" in host or host == "localhost"):
        raise ValueError(
            f"{entry!r} must start with a registry host (e.g. 'docker.io/library/', "
            "'ghcr.io/acme/')"
        )
    return f"{canon_host(host)}/{rest}"


def ref_violation(
    ref: str,
    tenant_id: uuid.UUID,
    registries: Iterable[RegistryNamespace],
    allowlist: Iterable[str],
) -> str | None:
    """Why ``tenant_id`` may not use ``ref``, or None when it may."""
    target = canonical(ref)
    registries = list(registries)

    in_own = False
    for registry in registries:
        if any(target.startswith(root) for root in own_roots(registry, tenant_id)):
            in_own = True
            break
        if any(target.startswith(root) for root in managed_roots(registry)):
            # Deliberately generic: it must not confirm that another tenant's image exists.
            return (
                "this reference is inside the platform's managed image namespace and does "
                "not belong to this organization"
            )

    prefixes = [normalise_allowlist_entry(e) for e in allowlist if e.strip()]
    if prefixes and not in_own and not any(target.startswith(p) for p in prefixes):
        return (
            "this deployment only allows runtime images from: "
            + ", ".join(prefixes)
            + " -- ask your administrator, or use an image built for your organization"
        )
    return None


# ── loaded check ───────────────────────────────────────────────────────────────────────

_CACHE_SECONDS = 30.0
_cache: tuple[float, list[RegistryNamespace], list[str]] | None = None


def invalidate_cache() -> None:
    """Called by the writers, so a change made in this process applies immediately; other
    processes see it within the cache window."""
    global _cache
    _cache = None


async def _load() -> tuple[list[RegistryNamespace], list[str]]:
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_SECONDS:
        return _cache[1], _cache[2]
    from core.images.policy import get_runtime_image_allowlist
    from core.images.registries import list_registry_namespaces

    registries = await list_registry_namespaces()
    allowlist = await get_runtime_image_allowlist()
    _cache = (now, registries, allowlist)
    return registries, allowlist


async def check_image_ref_for_tenant(tenant_id: uuid.UUID, ref: str | None) -> None:
    """Raise ``ImageRefError`` when ``tenant_id`` may not use ``ref``. Empty passes (it
    means "use the catalog default", which the catalog decides)."""
    if not ref or not ref.strip():
        return
    registries, allowlist = await _load()
    problem = ref_violation(ref, tenant_id, registries, allowlist)
    if problem:
        raise ImageRefError(problem)
