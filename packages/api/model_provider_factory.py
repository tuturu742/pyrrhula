"""The one place a concrete ``ModelProvider`` adapter is selected (T0.8). ``core`` never
imports ``adapters`` directly — it depends on the ``ModelProvider`` port and takes a
factory as a parameter; this is that factory, wired at the api layer (the composition
root), matching ``core.process.skeleton.ModelProviderFactory``.
"""

from __future__ import annotations

from adapters.models.echo.provider import EchoModelProvider
from adapters.models.litellm.provider import LiteLLMModelProvider
from core.ports.model_provider import ModelProvider


def get_model_provider(provider_name: str) -> ModelProvider:
    if provider_name == "echo":
        return EchoModelProvider()
    return LiteLLMModelProvider()
