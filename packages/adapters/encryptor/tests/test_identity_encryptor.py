from adapters.encryptor.identity import IdentityEncryptor
from core.ports.encryptor import Encryptor


def test_round_trips_unchanged() -> None:
    enc: Encryptor = IdentityEncryptor()
    assert enc.decrypt(enc.encrypt("some secret text")) == "some secret text"
