"""The one place a concrete ``ModerationProvider`` adapter is selected in the worker
(E2.2/G4.14), mirroring the API's composition root (CLAUDE.md rule 12).

Resolved per tenant, same as the API. A worker job always knows whose work it is running,
so it passes that in rather than falling back to a deployment-wide answer.
"""

from __future__ import annotations

import uuid

from core.moderation_selection import build_provider, moderation_choice
from core.ports.moderation import ModerationProvider


async def get_moderation_provider(
    tenant_id: uuid.UUID | None = None, workspace_id: uuid.UUID | None = None
) -> ModerationProvider:
    model, api_base = await moderation_choice(tenant_id, workspace_id)
    return build_provider(model, api_base)
