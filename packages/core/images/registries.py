"""Registries the operator has declared -- the only module that writes ``image_registry``.

Admin-only by construction: nothing here is reachable from a tenant route, a model-facing
tool or a pack (``tests/architecture/test_image_registries_are_operator_scoped.py``).

A registry's credential is **read** access -- enough to verify that a digest exists and to
let an engine pull it. It is sealed onto the admin tenant's ``provider_credential`` rows,
recorded with the registry host it belongs to, and never returned by any API.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError

from core.images.models import ImageRegistryRow
from core.images.namespace import RegistryNamespace, canon_host, invalidate_cache
from core.ports.encryptor import Encryptor
from core.tenancy.scope import unscoped_session

_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
# host[:port] -- no scheme, no path, no userinfo. A URL here would be a different thing to
# validate, and userinfo in a host field is how a credential ends up in a log line.
_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?$")
_PREFIX = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)*$")
_PULL_SECRET = re.compile(r"^([a-z0-9]([-a-z0-9]*[a-z0-9])?)?$")
_MAX_ALIASES = 8
_DOCKER_HUB = "docker.io"

MUTABLE_FIELDS = frozenset(
    {
        "label",
        "pull_host",
        "aliases",
        "path_prefix",
        "path_style",
        "insecure",
        "k8s_pull_secret",
        "supports_delete",
        "public_by_default_ack",
        "enabled",
    }
)


class InvalidRegistryError(ValueError):
    """A registry declaration that cannot be used. Always names the field."""


class RegistryNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class Registry:
    key: str
    label: str
    pull_host: str
    aliases: tuple[str, ...]
    path_prefix: str
    path_style: str
    insecure: bool
    has_credential: bool
    credential_username: str
    k8s_pull_secret: str
    supports_delete: bool
    public_by_default_ack: bool
    enabled: bool

    def namespace(self) -> RegistryNamespace:
        return RegistryNamespace(
            key=self.key,
            pull_host=self.pull_host,
            path_prefix=self.path_prefix,
            path_style=self.path_style,
            aliases=self.aliases,
        )


def _to_registry(row: ImageRegistryRow) -> Registry:
    return Registry(
        key=row.key,
        label=row.label,
        pull_host=row.pull_host,
        aliases=tuple(str(a) for a in (row.aliases or [])),
        path_prefix=row.path_prefix,
        path_style=row.path_style,
        insecure=row.insecure,
        has_credential=row.credential_ref is not None,
        credential_username=row.credential_username,
        k8s_pull_secret=row.k8s_pull_secret,
        supports_delete=row.supports_delete,
        public_by_default_ack=row.public_by_default_ack,
        enabled=row.enabled,
    )


def _host(value: Any, field_name: str) -> str:
    host = str(value or "").strip().lower()
    if not _HOST.match(host):
        raise InvalidRegistryError(
            f"{field_name}: expected host[:port] with no scheme or path, e.g. "
            "'registry.example.com' or 'localhost:5000'"
        )
    return canon_host(host)


def validate_fields(fields: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    """Check a create or update payload and return the storable values."""
    out: dict[str, Any] = {}
    unknown = set(fields) - MUTABLE_FIELDS
    if unknown:
        raise InvalidRegistryError(f"unknown field(s): {sorted(unknown)}")
    if "label" in fields:
        out["label"] = str(fields["label"] or "").strip()[:120]
    if "pull_host" in fields or not partial:
        out["pull_host"] = _host(fields.get("pull_host"), "pull_host")
    if "aliases" in fields:
        raw = fields["aliases"] or []
        if not isinstance(raw, list) or len(raw) > _MAX_ALIASES:
            raise InvalidRegistryError(f"aliases: a list of at most {_MAX_ALIASES} hosts")
        out["aliases"] = [_host(a, "aliases") for a in raw]
    if "path_prefix" in fields or not partial:
        prefix = str(fields.get("path_prefix") or "pyrrhula").strip().strip("/").lower()
        if not _PREFIX.match(prefix) or len(prefix) > 120:
            raise InvalidRegistryError(
                "path_prefix: lowercase path segments such as 'pyrrhula' or 'acme/builds'"
            )
        out["path_prefix"] = prefix
    if "path_style" in fields or not partial:
        style = str(fields.get("path_style") or "nested")
        if style not in ("nested", "flat"):
            raise InvalidRegistryError("path_style: 'nested' or 'flat'")
        out["path_style"] = style
    for flag in ("insecure", "supports_delete", "public_by_default_ack", "enabled"):
        if flag in fields:
            out[flag] = bool(fields[flag])
    if "k8s_pull_secret" in fields:
        secret = str(fields["k8s_pull_secret"] or "").strip()
        if len(secret) > 253 or not _PULL_SECRET.match(secret):
            raise InvalidRegistryError("k8s_pull_secret: a Kubernetes Secret name, or empty")
        out["k8s_pull_secret"] = secret
    return out


def _check_coherent(row: ImageRegistryRow) -> None:
    """Rules that span fields, checked on the final state rather than the payload."""
    if row.pull_host == _DOCKER_HUB:
        if row.path_style != "flat":
            raise InvalidRegistryError(
                "Docker Hub allows no nested repository paths: set path_style to 'flat' and "
                "path_prefix to your Docker Hub organization"
            )
        if "/" in row.path_prefix:
            raise InvalidRegistryError("path_prefix: on Docker Hub, just the organization name")
        if not row.public_by_default_ack:
            raise InvalidRegistryError(
                "Docker Hub creates new repositories as public by default: acknowledge "
                "public_by_default_ack, or make the organization's default private first"
            )


async def list_registries() -> list[Registry]:
    try:
        async with unscoped_session() as session:
            rows = (
                await session.execute(select(ImageRegistryRow).order_by(ImageRegistryRow.key))
            ).scalars()
            return [_to_registry(r) for r in rows]
    except ProgrammingError:  # before migrations
        return []


async def list_registry_namespaces() -> list[RegistryNamespace]:
    """Every declared registry, enabled or not: a disabled registry still owns its
    namespace, or disabling one would make its tenants' images anyone's to name."""
    return [r.namespace() for r in await list_registries()]


async def get_registry(key: str) -> Registry:
    async with unscoped_session() as session:
        row = await session.get(ImageRegistryRow, key)
        if row is None:
            raise RegistryNotFoundError(key)
        return _to_registry(row)


async def create_registry(key: str, fields: dict[str, Any]) -> Registry:
    key = (key or "").strip()
    if not _KEY.match(key):
        raise InvalidRegistryError("key: lowercase letters, digits and '-', at most 40")
    values = validate_fields(fields)
    async with unscoped_session() as session:
        if await session.get(ImageRegistryRow, key) is not None:
            raise InvalidRegistryError(f"a registry {key!r} already exists")
        row = ImageRegistryRow(key=key, **values)
        _check_coherent(row)
        session.add(row)
        await session.flush()
        registry = _to_registry(row)
    invalidate_cache()
    return registry


async def update_registry(key: str, fields: dict[str, Any]) -> Registry:
    values = validate_fields(fields, partial=True)
    async with unscoped_session() as session:
        row = await session.get(ImageRegistryRow, key)
        if row is None:
            raise RegistryNotFoundError(key)
        for name, value in values.items():
            setattr(row, name, value)
        _check_coherent(row)
        await session.flush()
        registry = _to_registry(row)
    invalidate_cache()
    return registry


async def delete_registry(key: str) -> None:
    from sqlalchemy.exc import IntegrityError

    try:
        async with unscoped_session() as session:
            row = await session.get(ImageRegistryRow, key)
            if row is None:
                raise RegistryNotFoundError(key)
            await session.delete(row)
    except IntegrityError as exc:
        raise InvalidRegistryError(
            f"a builder still pushes to {key!r}; remove or repoint it first"
        ) from exc
    invalidate_cache()


async def set_registry_credential(
    key: str, username: str, password: str, *, encryptor: Encryptor
) -> Registry:
    """Seal a read credential onto the admin tenant, bound to this registry's host.

    Rotation replaces the reference; the old sealed row is left behind (the same as every
    other credential in the platform today) rather than deleted from a tenant-scoped table
    by a path that has no tenant.
    """
    from core.agents.authoring import store_provider_credential
    from core.tenancy.admin import ADMIN_TENANT_ID

    if not password:
        raise InvalidRegistryError("password: required (a token works)")
    registry = await get_registry(key)
    payload = json.dumps(
        {
            "username": username or "",
            "password": password,
            "serveraddress": registry.pull_host,
        }
    )
    ref = await store_provider_credential(ADMIN_TENANT_ID, payload, encryptor=encryptor)
    async with unscoped_session() as session:
        row = await session.get(ImageRegistryRow, key)
        if row is None:
            raise RegistryNotFoundError(key)
        row.credential_ref = ref
        row.credential_username = (username or "")[:255]
        await session.flush()
        return _to_registry(row)


async def clear_registry_credential(key: str) -> Registry:
    async with unscoped_session() as session:
        row = await session.get(ImageRegistryRow, key)
        if row is None:
            raise RegistryNotFoundError(key)
        row.credential_ref = None
        row.credential_username = ""
        await session.flush()
        return _to_registry(row)


async def registry_read_credential(key: str, *, encryptor: Encryptor) -> dict[str, str] | None:
    """The decrypted read credential, for the caller's one request only. Never logged."""
    from core.agents.authoring import resolve_connection_api_key
    from core.tenancy.admin import ADMIN_TENANT_ID

    async with unscoped_session() as session:
        row = await session.get(ImageRegistryRow, key)
        ref: uuid.UUID | None = row.credential_ref if row is not None else None
    if ref is None:
        return None
    raw = await resolve_connection_api_key(ADMIN_TENANT_ID, str(ref), encryptor=encryptor)
    if not raw:
        return None
    data = json.loads(raw)
    return {
        "username": str(data.get("username") or ""),
        "password": str(data.get("password") or ""),
    }
