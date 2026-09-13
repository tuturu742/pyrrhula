"""The one place a concrete ``ModerationProvider`` adapter is selected (E2.2/G4.14).
Mirrors ``api.encryptor_factory``'s composition-root pattern (CLAUDE.md rule 12).
``PYRRHULA_MODERATION_MODEL`` set -> the model-backed classifier; empty -> allow-all.
"""

from __future__ import annotations

from adapters.moderation.allow_all import AllowAllModerationProvider
from adapters.moderation.model_backed import ModelBackedModerationProvider
from core.config import get_settings
from core.ports.moderation import ModerationProvider


def get_moderation_provider() -> ModerationProvider:
    settings = get_settings()
    if settings.moderation_model:
        from adapters.models.litellm.provider import LiteLLMModelProvider

        return ModelBackedModerationProvider(
            provider=LiteLLMModelProvider(),
            model=settings.moderation_model,
            api_base=settings.moderation_api_base or None,
        )
    return AllowAllModerationProvider()
