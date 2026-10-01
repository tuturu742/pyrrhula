"""Admin console: the container registries this deployment declares, and the allowlist of
where runtime images may come from.

Platform-admin only (the router depends on ``require_platform_admin``), and every change is
an audited event on the admin tenant's chain. Credentials are write-only: an admin can set
or clear one, and the API will say *that* one is set and for which username, never what it
is.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.encryptor_factory import get_encryptor
from api.image_registry_factory import get_registry_client
from api.routes.admin import _audit_admin, require_platform_admin
from core.images.policy import get_runtime_image_allowlist, set_runtime_image_allowlist
from core.images.registries import (
    InvalidRegistryError,
    Registry,
    RegistryNotFoundError,
    clear_registry_credential,
    create_registry,
    delete_registry,
    get_registry,
    list_registries,
    registry_read_credential,
    set_registry_credential,
    update_registry,
)
from core.ports.image_registry import RegistryAuth
from core.tenancy.admin import ADMIN_TENANT_ID

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_platform_admin)])


class RegistryOut(BaseModel):
    key: str
    label: str
    pull_host: str
    aliases: list[str]
    path_prefix: str
    path_style: str
    insecure: bool
    has_credential: bool
    credential_username: str
    k8s_pull_secret: str
    supports_delete: bool
    public_by_default_ack: bool
    enabled: bool


def _out(registry: Registry) -> RegistryOut:
    return RegistryOut(
        key=registry.key,
        label=registry.label,
        pull_host=registry.pull_host,
        aliases=list(registry.aliases),
        path_prefix=registry.path_prefix,
        path_style=registry.path_style,
        insecure=registry.insecure,
        has_credential=registry.has_credential,
        credential_username=registry.credential_username,
        k8s_pull_secret=registry.k8s_pull_secret,
        supports_delete=registry.supports_delete,
        public_by_default_ack=registry.public_by_default_ack,
        enabled=registry.enabled,
    )


class RegistryFields(BaseModel):
    label: str | None = None
    pull_host: str | None = None
    aliases: list[str] | None = None
    path_prefix: str | None = None
    path_style: str | None = None
    insecure: bool | None = None
    k8s_pull_secret: str | None = None
    supports_delete: bool | None = None
    public_by_default_ack: bool | None = None
    enabled: bool | None = None

    def given(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if k in self.model_fields_set}


class CreateRegistryRequest(RegistryFields):
    key: str


class CredentialRequest(BaseModel):
    username: str = ""
    password: str


class ProbeOut(BaseModel):
    reachable: bool
    authenticated: bool | None
    detail: str


class AllowlistBody(BaseModel):
    prefixes: list[str]


@router.get("/image-registries")
async def list_registries_endpoint() -> list[RegistryOut]:
    return [_out(r) for r in await list_registries()]


@router.post("/image-registries", status_code=201)
async def create_registry_endpoint(body: CreateRegistryRequest) -> RegistryOut:
    fields = body.given()
    fields.pop("key", None)
    try:
        registry = await create_registry(body.key, fields)
    except InvalidRegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _audit_admin(
        ADMIN_TENANT_ID, "image.registry.create", "deployment", {"key": registry.key}
    )
    return _out(registry)


@router.patch("/image-registries/{key}")
async def update_registry_endpoint(key: str, body: RegistryFields) -> RegistryOut:
    try:
        registry = await update_registry(key, body.given())
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no registry {key!r}") from exc
    except InvalidRegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _audit_admin(
        ADMIN_TENANT_ID,
        "image.registry.update",
        "deployment",
        {"key": key, "fields": sorted(body.given())},
    )
    return _out(registry)


@router.delete("/image-registries/{key}", status_code=204)
async def delete_registry_endpoint(key: str) -> None:
    try:
        await delete_registry(key)
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no registry {key!r}") from exc
    await _audit_admin(ADMIN_TENANT_ID, "image.registry.delete", "deployment", {"key": key})


@router.put("/image-registries/{key}/credential")
async def set_credential_endpoint(key: str, body: CredentialRequest) -> RegistryOut:
    try:
        registry = await set_registry_credential(
            key, body.username, body.password, encryptor=get_encryptor()
        )
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no registry {key!r}") from exc
    except InvalidRegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # The username is not secret and helps an auditor; the password never enters the row.
    await _audit_admin(
        ADMIN_TENANT_ID,
        "image.registry.credential_set",
        "deployment",
        {"key": key, "username": body.username},
    )
    return _out(registry)


@router.delete("/image-registries/{key}/credential")
async def clear_credential_endpoint(key: str) -> RegistryOut:
    try:
        registry = await clear_registry_credential(key)
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no registry {key!r}") from exc
    await _audit_admin(
        ADMIN_TENANT_ID, "image.registry.credential_clear", "deployment", {"key": key}
    )
    return _out(registry)


@router.post("/image-registries/{key}/test")
async def test_registry_endpoint(key: str) -> ProbeOut:
    """Is it reachable, and does the stored credential work. Answers rather than raises:
    a registry that is down is a finding to show, not an error in the console."""
    try:
        registry = await get_registry(key)
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no registry {key!r}") from exc
    creds = await registry_read_credential(key, encryptor=get_encryptor())
    auth = RegistryAuth(creds["username"], creds["password"]) if creds else None
    result = await get_registry_client().probe(
        registry.pull_host, insecure=registry.insecure, auth=auth
    )
    return ProbeOut(
        reachable=result.reachable, authenticated=result.authenticated, detail=result.detail
    )


@router.get("/runtime-image-allowlist")
async def get_allowlist_endpoint() -> AllowlistBody:
    return AllowlistBody(prefixes=await get_runtime_image_allowlist())


@router.put("/runtime-image-allowlist")
async def set_allowlist_endpoint(body: AllowlistBody) -> AllowlistBody:
    try:
        stored = await set_runtime_image_allowlist(body.prefixes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _audit_admin(
        ADMIN_TENANT_ID,
        "image.allowlist.set",
        "deployment",
        {"prefixes": stored},
    )
    return AllowlistBody(prefixes=stored)
