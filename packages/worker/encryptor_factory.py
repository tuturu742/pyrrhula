"""The one place a concrete ``Encryptor`` adapter is selected for the worker --
deliberately duplicated
from ``api.encryptor_factory`` (Appendix B keeps api/worker siblings, neither
importing the other in production code).

With ``PYRRHULA_ENCRYPTION_KEY`` set (32 bytes, base64) every sealed credential uses
AES-256-GCM; without it the identity stub applies -- tolerated for local dev only, with
a loud warning, and refused entirely when ``PYRRHULA_REQUIRE_ENCRYPTION=1`` (the
shippable compose sets both).
"""

from __future__ import annotations

import os
import warnings

from adapters.encryptor.aesgcm import AesGcmEncryptor, EncryptionKeyError, load_key
from adapters.encryptor.identity import IdentityEncryptor
from core.ports.encryptor import Encryptor

_cached: Encryptor | None = None


def get_encryptor() -> Encryptor:
    global _cached  # noqa: PLW0603 -- one adapter per process, key never changes mid-run
    if _cached is not None:
        return _cached
    raw = os.environ.get("PYRRHULA_ENCRYPTION_KEY", "")
    if raw:
        _cached = AesGcmEncryptor(load_key(raw))
        return _cached
    if os.environ.get("PYRRHULA_REQUIRE_ENCRYPTION", "") == "1":
        raise EncryptionKeyError(
            "PYRRHULA_REQUIRE_ENCRYPTION=1 but PYRRHULA_ENCRYPTION_KEY is unset -- "
            "generate one with: openssl rand -base64 32"
        )
    warnings.warn(
        "PYRRHULA_ENCRYPTION_KEY not set: credentials are stored UNENCRYPTED. "
        "Local development only -- never ship this configuration.",
        stacklevel=2,
    )
    _cached = IdentityEncryptor()
    return _cached
