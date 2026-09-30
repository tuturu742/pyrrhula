"""Which coding harnesses this tenant may select, and who may change that.

A harness spec carries a shell command that runs inside an execution environment, so this
whole surface is operator-scoped: writes need ``manage_tenant``, the same level that
already governs deployment-shaped configuration. Reads are open to any authenticated
member, because a workspace admin choosing a persona's harness needs to see the list.

The shape is ``/repos/runtimes``': built-ins are the floor, a tenant entry with the same
key wins, and withholding is a registration that says no -- a built-in lives in code and
so cannot be deleted, only masked. See ``core.harness.registry``.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.authz import require_tenant_permission
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from core.tenancy.context import RequestContext

router = APIRouter(
    prefix="/harnesses",
    tags=["harnesses"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


class HarnessResponse(BaseModel):
    key: str
    image: str = ""
    setup_cmds: list[str] = []
    wire_format: str = "openai"
    licence: str = ""
    redistributable: bool = False
    # False for a deployment built-in, True once this tenant has registered its own entry
    # under that key -- what runs is the tenant's command, and a catalog that hid that
    # would be lying about what a delegation executes.
    tenant_owned: bool = False


class RegisterHarnessRequest(BaseModel):
    """A harness spec, as data. ``command`` is a template over a fixed placeholder
    whitelist (``core.harness.registry.PLACEHOLDERS``), never an expression language."""

    command: str
    image: str = ""
    setup_cmds: list[str] = []
    env: dict[str, str] = {}
    config_files: dict[str, str] = {}
    wire_format: str = "openai"
    licence: str = ""
    redistributable: bool = False


def _response(key: str, entry: dict[str, Any], *, tenant_owned: bool) -> HarnessResponse:
    return HarnessResponse(
        key=key,
        image=str(entry.get("image") or ""),
        setup_cmds=[str(c) for c in cast("list[object]", entry.get("setup_cmds") or [])],
        wire_format=str(entry.get("wire_format") or "openai"),
        licence=str(entry.get("licence") or ""),
        redistributable=bool(entry.get("redistributable")),
        tenant_owned=tenant_owned,
    )


@router.get("")
async def list_harnesses_endpoint(
    ctx: RequestContext = Depends(get_request_context),
) -> list[HarnessResponse]:
    """Every harness a persona in this tenant may be pointed at.

    Withheld ones are absent rather than listed with a flag: a caller asking "what may I
    use" should not have to remember to filter, which is how a forbidden capability ends
    up offered in a dropdown."""
    from core.harness.registry import BUILTIN_HARNESSES, resolved_harnesses

    effective = await resolved_harnesses(ctx.tenant_id)
    return [
        _response(key, entry, tenant_owned=BUILTIN_HARNESSES.get(key) != entry)
        for key, entry in sorted(effective.items())
    ]


@router.put("/{key}")
async def register_harness_endpoint(
    key: str,
    body: RegisterHarnessRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> HarnessResponse:
    """Register (or replace) one of this tenant's harnesses.

    This is the path an enterprise build arrives by -- a tailored Claude Code behind an
    internal registry needs no code from us, only a spec. Naming a built-in's key
    overrides it for this tenant."""
    from core.harness.registry import InvalidHarnessError, register_harness

    await require_tenant_permission(ctx, "manage_tenant")
    try:
        entry = await register_harness(ctx.tenant_id, key, body.model_dump())
    except InvalidHarnessError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _response(key, entry, tenant_owned=True)


@router.post("/{key}/withhold", status_code=204)
async def withhold_harness_endpoint(
    key: str, ctx: RequestContext = Depends(get_request_context)
) -> None:
    """Make a harness unavailable here, built-in included -- the "internal policy forbids
    it" case. Personas already naming it degrade to the one-shot codegen path and say so
    in the transcript; they are not broken by this."""
    from core.harness.registry import InvalidHarnessError, withhold_harness

    await require_tenant_permission(ctx, "manage_tenant")
    try:
        await withhold_harness(ctx.tenant_id, key)
    except InvalidHarnessError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/{key}", status_code=204)
async def remove_harness_endpoint(
    key: str, ctx: RequestContext = Depends(get_request_context)
) -> None:
    """Forget one of this tenant's entries, mask included. A built-in of the same name
    becomes available again, which is how a withholding is undone."""
    from core.harness.registry import remove_harness

    await require_tenant_permission(ctx, "manage_tenant")
    if not await remove_harness(ctx.tenant_id, key):
        raise HTTPException(status_code=404, detail=f"this tenant has no harness {key!r}")
