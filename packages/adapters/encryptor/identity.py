"""v1 Encryptor: the identity function. Swapped for per-tenant KMS/BYOK at H5.7 without
touching any call site."""

from __future__ import annotations


class IdentityEncryptor:
    def encrypt(self, plaintext: str) -> str:
        return plaintext

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext
