"""Real ``Encryptor``: AES-256-GCM with a deployment key.

Replaces the v1 identity stub for every sealed credential (provider API keys, repo
access tokens, registry passwords). Key: ``PYRRHULA_ENCRYPTION_KEY`` — 32 bytes,
base64 (``openssl rand -base64 32``).

Ciphertexts are version-prefixed (``enc1:<b64(nonce || ct+tag)>``) so rows written by
the identity era are recognizable: ``decrypt`` passes anything without the prefix
through unchanged (legacy plaintext keeps working), and the one-shot
``python -m core.credentials.reencrypt`` command wraps those legacy rows in place.
Per-tenant KMS/BYOK remains a later adapter swap behind the same port.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_PREFIX = "enc1:"
_NONCE_LEN = 12


class EncryptionKeyError(Exception):
    pass


def load_key(raw: str) -> bytes:
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise EncryptionKeyError("PYRRHULA_ENCRYPTION_KEY is not valid base64") from exc
    if len(key) != 32:
        raise EncryptionKeyError(
            f"PYRRHULA_ENCRYPTION_KEY must decode to 32 bytes (got {len(key)}); "
            "generate one with: openssl rand -base64 32"
        )
    return key


class AesGcmEncryptor:
    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(_NONCE_LEN)
        sealed = self._aead.encrypt(nonce, plaintext.encode(), None)
        return _PREFIX + base64.b64encode(nonce + sealed).decode()

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext.startswith(_PREFIX):
            # Identity-era row (or a caller that never sealed) -- pass through so
            # existing credentials keep working until `reencrypt` wraps them.
            return ciphertext
        raw = base64.b64decode(ciphertext[len(_PREFIX) :])
        nonce, sealed = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
        return self._aead.decrypt(nonce, sealed, None).decode()

    @staticmethod
    def is_sealed(ciphertext: str) -> bool:
        return ciphertext.startswith(_PREFIX)
