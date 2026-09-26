"""The no-op Encryptor: plaintext in, plaintext out. The development fallback; a
deployment sets ``PYRRHULA_REQUIRE_ENCRYPTION`` to refuse booting with it."""

from __future__ import annotations


class IdentityEncryptor:
    def encrypt(self, plaintext: str) -> str:
        return plaintext

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext
