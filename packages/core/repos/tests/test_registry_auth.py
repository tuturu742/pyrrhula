"""A repo's registry credential goes to the registry it was issued for, and nowhere else.

Before this, the credential was sent to ``image.split("/")[0]`` of whatever image the
delegation ran -- and that image can come from ``pyrrhula-build.json`` inside the
repository. A commit could point it at a host of its choosing and receive the password.
"""

from __future__ import annotations

import base64
import json

import pytest

from core.repos.image_ref import (
    ImageRefError,
    canonical,
    has_tag_or_digest,
    is_digest_pinned,
    normalise_image_ref,
    registry_host,
    serveraddress_for,
)
from core.repos.registry_auth import credential_applies, x_registry_auth

_DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("ref", "host"),
    [
        ("python:3.12", "docker.io"),
        ("barichello/godot-ci:4.2", "docker.io"),  # the old split said "barichello"
        ("docker.io/library/python:3.12", "docker.io"),
        ("index.docker.io/org/img:1", "docker.io"),
        ("ghcr.io/org/img:1", "ghcr.io"),
        ("localhost:5000/pyr/img:1", "localhost:5000"),
        ("localhost/img:1", "localhost"),
        ("registry.example.com:5001/a/b/c:1", "registry.example.com:5001"),
    ],
)
def test_the_registry_is_found_the_way_the_engine_finds_it(ref: str, host: str) -> None:
    assert registry_host(ref) == host


def test_two_spellings_of_one_image_canonicalise_together() -> None:
    assert canonical("python:3.12") == "docker.io/library/python:3.12"
    assert canonical("index.docker.io/library/python:3.12") == "docker.io/library/python:3.12"
    assert canonical("org/img:1") == "docker.io/org/img:1"
    assert canonical(f"python@{_DIGEST}") == f"docker.io/library/python@{_DIGEST}"
    assert canonical("ghcr.io/org/img:1") == "ghcr.io/org/img:1"


def test_docker_hub_credentials_are_keyed_by_the_index_url() -> None:
    assert serveraddress_for("docker.io") == "https://index.docker.io/v1/"
    assert serveraddress_for("ghcr.io") == "ghcr.io"


def test_a_port_is_not_a_tag() -> None:
    assert not has_tag_or_digest("localhost:5000/pyr/img")
    assert has_tag_or_digest("localhost:5000/pyr/img:1")
    assert has_tag_or_digest(f"img@{_DIGEST}")
    assert is_digest_pinned(f"ghcr.io/o/i@{_DIGEST}")
    assert not is_digest_pinned("ghcr.io/o/i:1")


def test_an_untagged_reference_is_refused_where_it_is_typed() -> None:
    """Asked for an untagged image, the engine pulls every tag of the repository."""
    with pytest.raises(ImageRefError, match="no tag"):
        normalise_image_ref("barichello/godot-ci")


_BOUND = {"username": "u", "password": "s3cret", "serveraddress": "registry.acme.io"}
_LEGACY = {"username": "u", "password": "s3cret"}


def test_a_bound_credential_reaches_its_own_registry() -> None:
    header = x_registry_auth(_BOUND, "registry.acme.io/team/img:1", image_source="manifest")
    assert header is not None
    payload = json.loads(base64.b64decode(header))
    assert payload["password"] == "s3cret"
    assert payload["serveraddress"] == "registry.acme.io"


def test_a_manifest_cannot_redirect_the_credential_elsewhere() -> None:
    """The attack, exactly: the repo's own pyrrhula-build.json names another host."""
    assert x_registry_auth(_BOUND, "evil.example/x:1", image_source="manifest") is None
    assert x_registry_auth(_BOUND, "evil/x:1", image_source="manifest") is None


def test_a_legacy_credential_only_follows_an_operator_typed_image() -> None:
    """Sealed before credentials recorded their registry, so nothing says where it belongs.
    Only an image the operator typed is trusted with it."""
    assert credential_applies(_LEGACY, "registry.acme.io/img:1", image_source="repo")
    assert not credential_applies(_LEGACY, "registry.acme.io/img:1", image_source="manifest")
    assert not credential_applies(_LEGACY, "registry.acme.io/img:1", image_source=None)


def test_docker_hub_short_references_get_the_right_serveraddress() -> None:
    bound = {"username": "u", "password": "p", "serveraddress": "docker.io"}
    header = x_registry_auth(bound, "org/name:1", image_source="repo")
    assert header is not None
    assert json.loads(base64.b64decode(header))["serveraddress"] == ("https://index.docker.io/v1/")
