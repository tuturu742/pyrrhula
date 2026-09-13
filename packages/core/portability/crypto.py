"""Password encryption for `.pyr` bundles.

Format: ``PYRENC1\\0`` magic + 16-byte scrypt salt + 12-byte AES-GCM nonce +
ciphertext (the sealed ZIP, whole). scrypt (stdlib) for the KDF -- memory-hard, no new
dependency -- and AES-256-GCM (the same primitive the at-rest encryptor uses) so
tampering fails decryption outright rather than yielding garbage ZIP bytes.

Why whole-file and not per-entry: the manifest itself lists redactions, secret gists,
and session refs -- metadata worth protecting as much as content. An encrypted bundle
reveals nothing but its own magic header and size.
"""

from __future__ import annotations

import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"PYRENC1\x00"
_SALT_LEN = 16
_NONCE_LEN = 12
# scrypt cost: ~64MB memory, interactive-grade latency. A bundle password guards
# provider API keys; this should cost an attacker real work per guess.
_SCRYPT_N = 2**16
_SCRYPT_R = 8
_SCRYPT_P = 1


class WrongPasswordError(Exception):
    """Decryption failed -- wrong password, or the file was tampered with (GCM cannot
    distinguish the two, and for the caller the remedy is the same)."""


def _derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=128 * 1024 * 1024,
        dklen=32,
    )


def is_encrypted(data: bytes) -> bool:
    return data.startswith(MAGIC)


def encrypt_bundle(data: bytes, password: str) -> bytes:
    if not password:
        raise ValueError("an empty password is not encryption")
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key = _derive_key(password, salt)
    ciphertext = AESGCM(key).encrypt(nonce, data, MAGIC)
    return MAGIC + salt + nonce + ciphertext


def decrypt_bundle(data: bytes, password: str) -> bytes:
    if not is_encrypted(data):
        raise ValueError("not an encrypted bundle")
    if not password:
        raise WrongPasswordError("this bundle is encrypted -- a password is required")
    offset = len(MAGIC)
    salt = data[offset : offset + _SALT_LEN]
    nonce = data[offset + _SALT_LEN : offset + _SALT_LEN + _NONCE_LEN]
    ciphertext = data[offset + _SALT_LEN + _NONCE_LEN :]
    key = _derive_key(password, salt)
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, MAGIC)
    except InvalidTag as exc:
        raise WrongPasswordError("wrong password (or the file is corrupted)") from exc
