"""The worker's composition root for ``ModelProvider`` selection — mirrors
``api.model_provider_factory`` (T0.8). ``core`` never imports ``adapters`` directly; this
is where the concrete choice is made. The ingestion job (A1.2) only needs
``count_tokens``, which is local/offline (tiktoken) regardless of provider, but going
through the same port keeps tokenizer selection consistent with generation/embedding.
"""

from __future__ import annotations

from adapters.models.echo.provider import EchoModelProvider
from adapters.models.litellm.provider import LiteLLMModelProvider
from core.ports.model_provider import ModelProvider


def get_model_provider(provider_name: str) -> ModelProvider:
    if provider_name == "echo":
        return EchoModelProvider()
    return LiteLLMModelProvider()
