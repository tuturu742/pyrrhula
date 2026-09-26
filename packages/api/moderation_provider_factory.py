"""The one place a concrete ``ModerationProvider`` adapter is selected.
Mirrors ``api.encryptor_factory``'s composition-root pattern (CLAUDE.md rule 12).

The choice is resolved **per tenant** (workspace setting, then tenant setting, then
allow-all): two tenants can reasonably want different moderation, and
a composition root is exactly where that selection belongs -- the port itself stays
tenant-agnostic.
"""

from __future__ import annotations

import uuid

from fastapi import Depends

from api.middleware.auth import get_request_context
from core.moderation_selection import build_provider, moderation_choice
from core.ports.moderation import ModerationProvider
from core.tenancy.context import RequestContext


async def moderation_provider_for(
    tenant_id: uuid.UUID | None, workspace_id: uuid.UUID | None = None
) -> ModerationProvider:
    """For callers holding a tenant directly (background tasks, internal helpers)."""
    model, api_base = await moderation_choice(tenant_id, workspace_id)
    return build_provider(model, api_base)


async def get_moderation_provider(
    ctx: RequestContext = Depends(get_request_context),
) -> ModerationProvider:
    """The FastAPI dependency. Kept separate from ``moderation_provider_for`` so a direct
    call cannot accidentally bind a tenant id to the injected request context."""
    return await moderation_provider_for(ctx.tenant_id)
