"""Builders the operator has declared -- the only module that writes ``image_builder``.

Admin-only by construction, like ``core.images.registries``: nothing here is reachable
from a tenant route, a model-facing tool or a pack. Organizations use what was declared
for them; they never declare one.

``config`` is per-kind data, validated here. The credential -- a webhook's signing secret
and optional token, later a CI token or a Portainer API key -- is sealed on the admin
tenant and never returned by the API.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError

from core.images.models import ImageBuilderRow, ImageRegistryRow
from core.ports.encryptor import Encryptor
from core.tenancy.scope import unscoped_session

_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
# The kinds this deployment can drive. The table admits the ones later phases add; a kind
# is only accepted here once its adapter exists.
SUPPORTED_KINDS = frozenset({"webhook", "github_actions", "portainer"})
# Builders that hold a connection for the whole build (core.ports.image_builder).
STREAM_KINDS = frozenset({"portainer"})
# Builders whose RUN steps execute on infrastructure the operator runs directly, where an
# acknowledged isolation probe is required before any organization may use them.
ACK_KINDS = frozenset({"portainer"})
_GH_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?$")
MUTABLE_FIELDS = frozenset(
    {"label", "config", "registry_key", "allowed_tenants", "isolation_ack", "enabled"}
)
_MAX_ALLOWED_TENANTS = 1000


class InvalidBuilderError(ValueError):
    """A builder declaration that cannot be used. Always names the field."""


class BuilderNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class Builder:
    key: str
    kind: str
    label: str
    config: dict[str, Any]
    registry_key: str
    allowed_tenants: tuple[str, ...] | None
    isolation_ack: bool
    enabled: bool
    has_credential: bool

    def allows(self, tenant_id: uuid.UUID) -> bool:
        return self.allowed_tenants is None or str(tenant_id) in self.allowed_tenants


def _to_builder(row: ImageBuilderRow) -> Builder:
    return Builder(
        key=row.key,
        kind=row.kind,
        label=row.label,
        config=dict(row.config or {}),
        registry_key=row.registry_key,
        allowed_tenants=(
            tuple(str(t) for t in row.allowed_tenants) if row.allowed_tenants is not None else None
        ),
        isolation_ack=row.isolation_ack,
        enabled=row.enabled,
        has_credential=row.credential_ref is not None,
    )


def _url(value: Any, field_name: str, *, insecure: bool, required: bool = True) -> str:
    text = str(value or "").strip()
    if not text and not required:
        return ""
    if len(text) > 1000 or any(c.isspace() for c in text):
        raise InvalidBuilderError(f"{field_name}: not a URL")
    scheme, sep, rest = text.partition("://")
    if not sep or scheme not in ("https", "http"):
        raise InvalidBuilderError(f"{field_name}: an https:// URL")
    if scheme == "http" and not insecure:
        raise InvalidBuilderError(f"{field_name}: https is required unless 'insecure' is set")
    authority = rest.split("/", 1)[0]
    if "@" in authority:
        # Userinfo in a URL is how a credential ends up in a log line.
        raise InvalidBuilderError(f"{field_name}: no credentials in the URL; use the credential")
    if not authority:
        raise InvalidBuilderError(f"{field_name}: no host")
    return text


def validate_config(kind: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise InvalidBuilderError("config: an object")
    insecure = bool(raw.get("insecure", False))
    if kind == "webhook":
        unknown = set(raw) - {"submit_url", "status_url", "cancel_url", "insecure"}
        if unknown:
            raise InvalidBuilderError(f"config: unknown field(s) {sorted(unknown)}")
        return {
            "submit_url": _url(raw.get("submit_url"), "config.submit_url", insecure=insecure),
            "status_url": _url(raw.get("status_url"), "config.status_url", insecure=insecure),
            "cancel_url": _url(
                raw.get("cancel_url"), "config.cancel_url", insecure=insecure, required=False
            ),
            "insecure": insecure,
        }
    if kind == "github_actions":
        unknown = set(raw) - {"owner", "repo", "workflow", "ref", "api_base"}
        if unknown:
            raise InvalidBuilderError(f"config: unknown field(s) {sorted(unknown)}")
        out: dict[str, Any] = {}
        for name in ("owner", "repo", "workflow", "ref"):
            value = str(raw.get(name) or ("main" if name == "ref" else "")).strip()
            if not _GH_NAME.match(value):
                raise InvalidBuilderError(f"config.{name}: letters, digits, '.', '_' and '-' only")
            out[name] = value
        # GitHub Enterprise Server is the same API under another base.
        out["api_base"] = _url(
            raw.get("api_base") or "https://api.github.com", "config.api_base", insecure=False
        ).rstrip("/")
        return out
    if kind == "portainer":
        allowed = {
            "base_url",
            "endpoint_id",
            "push_host",
            "tls_verify",
            "network_mode",
            "memory_mb",
            "cpus",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise InvalidBuilderError(f"config: unknown field(s) {sorted(unknown)}")
        base_url = _url(raw.get("base_url"), "config.base_url", insecure=False).rstrip("/")
        try:
            endpoint_id = int(str(raw.get("endpoint_id")))
        except (TypeError, ValueError) as exc:
            raise InvalidBuilderError("config.endpoint_id: the Portainer environment id") from exc
        push_host = str(raw.get("push_host") or "").strip().lower()
        if push_host and not _HOST.match(push_host):
            raise InvalidBuilderError("config.push_host: host[:port], e.g. 127.0.0.1:5002")
        network_mode = str(raw.get("network_mode") or "").strip()
        if network_mode and not _GH_NAME.match(network_mode):
            raise InvalidBuilderError("config.network_mode: a Docker network name")
        if network_mode == "host":
            # RUN steps on the host's network would see every loopback service on the
            # engine host -- the opposite of what a build network is for.
            raise InvalidBuilderError("config.network_mode: 'host' is not allowed")
        try:
            memory_mb = int(raw.get("memory_mb") or 4096)
            cpus = float(raw.get("cpus") or 2)
        except (TypeError, ValueError) as exc:
            raise InvalidBuilderError("config.memory_mb / cpus: numbers") from exc
        if not 256 <= memory_mb <= 65536 or not 0.25 <= cpus <= 64:
            raise InvalidBuilderError("config: memory_mb 256-65536, cpus 0.25-64")
        return {
            "base_url": base_url,
            "endpoint_id": endpoint_id,
            "push_host": push_host,
            "tls_verify": bool(raw.get("tls_verify", True)),
            "network_mode": network_mode,
            "memory_mb": memory_mb,
            "cpus": cpus,
        }
    raise InvalidBuilderError(f"kind {kind!r} is not supported by this deployment")


def _allowed(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > _MAX_ALLOWED_TENANTS:
        raise InvalidBuilderError("allowed_tenants: null (everyone) or a list of tenant ids")
    out: list[str] = []
    for value in raw:
        try:
            out.append(str(uuid.UUID(str(value))))
        except ValueError as exc:
            raise InvalidBuilderError(f"allowed_tenants: {value!r} is not a tenant id") from exc
    return sorted(set(out))


async def _check_registry(session: Any, key: str) -> None:
    if await session.get(ImageRegistryRow, key) is None:
        raise InvalidBuilderError(f"registry_key: no declared registry {key!r}")


async def list_builders() -> list[Builder]:
    try:
        async with unscoped_session() as session:
            rows = (
                await session.execute(select(ImageBuilderRow).order_by(ImageBuilderRow.key))
            ).scalars()
            return [_to_builder(r) for r in rows]
    except ProgrammingError:  # before migrations
        return []


async def get_builder(key: str) -> Builder:
    async with unscoped_session() as session:
        row = await session.get(ImageBuilderRow, key)
        if row is None:
            raise BuilderNotFoundError(key)
        return _to_builder(row)


async def builders_for_tenant(tenant_id: uuid.UUID) -> list[Builder]:
    """Builders this organization may use right now: enabled, allowed, credentialed,
    pushing to an enabled registry -- and, for an engine the operator runs directly, with
    its isolation probe acknowledged."""
    from core.images.registries import list_registries

    enabled_registries = {r.key for r in await list_registries() if r.enabled}
    return [
        b
        for b in await list_builders()
        if b.enabled
        and b.has_credential
        and b.allows(tenant_id)
        and b.registry_key in enabled_registries
        and (b.kind not in ACK_KINDS or b.isolation_ack)
    ]


async def create_builder(key: str, kind: str, fields: dict[str, Any]) -> Builder:
    key = (key or "").strip()
    if not _KEY.match(key):
        raise InvalidBuilderError("key: lowercase letters, digits and '-', at most 40")
    if kind not in SUPPORTED_KINDS:
        raise InvalidBuilderError(
            f"kind: one of {sorted(SUPPORTED_KINDS)} (this deployment cannot drive {kind!r})"
        )
    unknown = set(fields) - MUTABLE_FIELDS
    if unknown:
        raise InvalidBuilderError(f"unknown field(s): {sorted(unknown)}")
    config = validate_config(kind, fields.get("config") or {})
    registry_key = str(fields.get("registry_key") or "")
    async with unscoped_session() as session:
        if await session.get(ImageBuilderRow, key) is not None:
            raise InvalidBuilderError(f"a builder {key!r} already exists")
        await _check_registry(session, registry_key)
        row = ImageBuilderRow(
            key=key,
            kind=kind,
            label=str(fields.get("label") or "").strip()[:120],
            config=config,
            registry_key=registry_key,
            allowed_tenants=_allowed(fields.get("allowed_tenants")),
            isolation_ack=bool(fields.get("isolation_ack", False)),
            enabled=bool(fields.get("enabled", True)),
        )
        session.add(row)
        await session.flush()
        return _to_builder(row)


async def update_builder(key: str, fields: dict[str, Any]) -> Builder:
    unknown = set(fields) - MUTABLE_FIELDS
    if unknown:
        raise InvalidBuilderError(f"unknown field(s): {sorted(unknown)}")
    async with unscoped_session() as session:
        row = await session.get(ImageBuilderRow, key)
        if row is None:
            raise BuilderNotFoundError(key)
        if "label" in fields:
            row.label = str(fields["label"] or "").strip()[:120]
        if "config" in fields:
            new_config = validate_config(row.kind, fields["config"])
            if row.kind in ACK_KINDS and new_config != row.config and "isolation_ack" not in fields:
                # A different engine or network is a different answer to "what can a
                # tenant's RUN step reach"; the old acknowledgement does not cover it.
                row.isolation_ack = False
            row.config = new_config
        if "registry_key" in fields:
            await _check_registry(session, str(fields["registry_key"] or ""))
            row.registry_key = str(fields["registry_key"])
        if "allowed_tenants" in fields:
            row.allowed_tenants = _allowed(fields["allowed_tenants"])
        for flag in ("isolation_ack", "enabled"):
            if flag in fields:
                setattr(row, flag, bool(fields[flag]))
        await session.flush()
        return _to_builder(row)


async def delete_builder(key: str) -> None:
    async with unscoped_session() as session:
        row = await session.get(ImageBuilderRow, key)
        if row is None:
            raise BuilderNotFoundError(key)
        await session.delete(row)


def _credential_fields(kind: str, raw: dict[str, Any]) -> dict[str, str]:
    if kind == "webhook":
        secret = str(raw.get("signing_secret") or "")
        if len(secret) < 16:
            raise InvalidBuilderError(
                "signing_secret: at least 16 characters -- it is what lets the receiver "
                "refuse anyone who is not this deployment"
            )
        return {"signing_secret": secret, "token": str(raw.get("token") or "")}
    if kind == "github_actions":
        token = str(raw.get("token") or "")
        if len(token) < 20:
            raise InvalidBuilderError(
                "token: a fine-grained token with Actions read and write on the build "
                "repository only"
            )
        return {"token": token}
    if kind == "portainer":
        api_key = str(raw.get("api_key") or "")
        if len(api_key) < 20:
            raise InvalidBuilderError(
                "api_key: a Portainer API key for a user with access to this environment "
                "only (not an administrator)"
            )
        password = str(raw.get("push_password") or "")
        if not password:
            raise InvalidBuilderError(
                "push_password: the engine pushes to the registry with this; it is sent "
                "on the push request only"
            )
        return {
            "api_key": api_key,
            "push_username": str(raw.get("push_username") or ""),
            "push_password": password,
        }
    raise InvalidBuilderError(f"kind {kind!r} is not supported by this deployment")


async def set_builder_credential(key: str, raw: dict[str, Any], *, encryptor: Encryptor) -> Builder:
    """Seal the builder's credential on the admin tenant. Rotation replaces the reference."""
    from core.agents.authoring import store_provider_credential
    from core.tenancy.admin import ADMIN_TENANT_ID

    builder = await get_builder(key)
    payload = json.dumps(_credential_fields(builder.kind, raw))
    ref = await store_provider_credential(ADMIN_TENANT_ID, payload, encryptor=encryptor)
    async with unscoped_session() as session:
        row = await session.get(ImageBuilderRow, key)
        if row is None:
            raise BuilderNotFoundError(key)
        row.credential_ref = ref
        await session.flush()
        return _to_builder(row)


async def clear_builder_credential(key: str) -> Builder:
    async with unscoped_session() as session:
        row = await session.get(ImageBuilderRow, key)
        if row is None:
            raise BuilderNotFoundError(key)
        row.credential_ref = None
        await session.flush()
        return _to_builder(row)


async def builder_secrets(key: str, *, encryptor: Encryptor) -> dict[str, str]:
    """The decrypted credential, for the caller's one use only. Never logged."""
    from core.agents.authoring import resolve_connection_api_key
    from core.tenancy.admin import ADMIN_TENANT_ID

    async with unscoped_session() as session:
        row = await session.get(ImageBuilderRow, key)
        ref: uuid.UUID | None = row.credential_ref if row is not None else None
    if ref is None:
        return {}
    raw = await resolve_connection_api_key(ADMIN_TENANT_ID, str(ref), encryptor=encryptor)
    if not raw:
        return {}
    data = json.loads(raw)
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
