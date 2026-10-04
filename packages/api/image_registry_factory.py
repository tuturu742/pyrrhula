"""Composition root for the ``RegistryClient`` port (rule 12)."""

from __future__ import annotations

from adapters.image_registry.registry_v2 import RegistryV2Client
from core.ports.image_registry import RegistryClient


def get_registry_client() -> RegistryClient:
    return RegistryV2Client()
