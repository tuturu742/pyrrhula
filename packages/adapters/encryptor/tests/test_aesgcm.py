from __future__ import annotations

import base64
import os

import pytest

from adapters.encryptor.aesgcm import AesGcmEncryptor, EncryptionKeyError, load_key


def _enc() -> AesGcmEncryptor:
    return AesGcmEncryptor(os.urandom(32))


def test_round_trip() -> None:
    enc = _enc()
    sealed = enc.encrypt("sk-secret-key")
    assert sealed != "sk-secret-key" and sealed.startswith("enc1:")
    assert enc.decrypt(sealed) == "sk-secret-key"


def test_nonces_differ() -> None:
    enc = _enc()
    assert enc.encrypt("x") != enc.encrypt("x")


def test_legacy_plaintext_passes_through() -> None:
    # Identity-era rows have no prefix -- decrypt returns them untouched.
    assert _enc().decrypt("legacy-plaintext-token") == "legacy-plaintext-token"
    assert not AesGcmEncryptor.is_sealed("legacy-plaintext-token")


def test_tamper_fails() -> None:
    enc = _enc()
    sealed = enc.encrypt("secret")
    raw = bytearray(base64.b64decode(sealed[len("enc1:") :]))
    raw[-1] ^= 0xFF
    with pytest.raises(Exception):  # noqa: B017 -- InvalidTag from cryptography
        enc.decrypt("enc1:" + base64.b64encode(bytes(raw)).decode())


def test_wrong_key_fails() -> None:
    sealed = _enc().encrypt("secret")
    with pytest.raises(Exception):  # noqa: B017
        _enc().decrypt(sealed)


def test_load_key_validation() -> None:
    assert len(load_key(base64.b64encode(os.urandom(32)).decode())) == 32
    with pytest.raises(EncryptionKeyError):
        load_key("not-base64!!")
    with pytest.raises(EncryptionKeyError):
        load_key(base64.b64encode(b"short").decode())
