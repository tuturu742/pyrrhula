"""Which moderation model a tenant's content is checked against.

The port stays a port (CLAUDE.md rule 12): ``ModerationProvider.check()`` is unchanged and
knows nothing about tenants. What changes is the *composition root* -- the factory that
picks an adapter now resolves the model for the tenant it is building for, which is where
a selection decision belongs.

Two tenants can reasonably want different moderation, so the model is a setting on the
chain (``core/settings/resolve.py``): workspace, then tenant, then nothing -- a
deployment that configured no classifier anywhere screens nothing, honestly.
"""

from __future__ import annotations

import uuid

from core.ports.moderation import ModerationProvider

MODERATION_MODEL_KEY = "moderation_model"
MODERATION_API_BASE_KEY = "moderation_api_base"


async def moderation_choice(
    tenant_id: uuid.UUID | None, workspace_id: uuid.UUID | None = None
) -> tuple[str, str | None]:
    """``(model, api_base)`` for this tenant -- ``("", None)`` meaning allow-all.

    ``tenant_id`` is optional for the composition roots that genuinely have no tenant in
    hand (startup wiring, a health check); those get allow-all rather than a fabricated
    tenant.
    """
    model = ""
    api_base = ""

    if tenant_id is not None:
        from core.settings.resolve import resolved_settings

        effective = await resolved_settings(
            tenant_id,
            workspace_id,
            {MODERATION_MODEL_KEY: model, MODERATION_API_BASE_KEY: api_base},
        )
        model = str(effective[MODERATION_MODEL_KEY] or "")
        api_base = str(effective[MODERATION_API_BASE_KEY] or "")

    return model, (api_base or None)


def build_provider(model: str, api_base: str | None) -> ModerationProvider:
    """The adapter for a resolved choice. Empty model means allow-all, which is the
    honest default: a deployment that configured no classifier must not silently get one."""
    from adapters.moderation.allow_all import AllowAllModerationProvider

    if not model:
        return AllowAllModerationProvider()

    from adapters.models.litellm.provider import LiteLLMModelProvider
    from adapters.moderation.model_backed import ModelBackedModerationProvider

    return ModelBackedModerationProvider(
        provider=LiteLLMModelProvider(), model=model, api_base=api_base
    )
