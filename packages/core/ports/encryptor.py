"""Encryptor port (D11, §14.3). v1 is the identity function — call sites (starting with
``secret.content``, Phase 2) go through this port from day one so per-tenant KMS/BYOK
(H5.7) is an adapter swap, not a retrofit touching every write path.
"""

from __future__ import annotations

from typing import Protocol


class Encryptor(Protocol):
    def encrypt(self, plaintext: str) -> str: ...
    def decrypt(self, ciphertext: str) -> str: ...
