"""The registry client against a mock registry: challenge flows, digests, refusals."""

from __future__ import annotations

import base64

import httpx
import pytest

from adapters.image_registry.registry_v2 import RegistryV2Client, split_ref
from core.net_guard import BlockedAddressError, check_ip
from core.ports.image_registry import RegistryAuth, RegistryError

DIGEST = "sha256:" + "c" * 64


def test_references_split_into_host_repository_and_reference() -> None:
    assert split_ref("python:3.12") == ("docker.io", "library/python", "3.12")
    assert split_ref("localhost:5000/a/b") == ("localhost:5000", "a/b", "latest")
    assert split_ref(f"ghcr.io/o/i@{DIGEST}") == ("ghcr.io", "o/i", DIGEST)


def _bearer_registry(expect_user: str | None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.test":
            if expect_user:
                basic = request.headers.get("Authorization", "")
                user = base64.b64decode(basic.removeprefix("Basic ")).decode().split(":")[0]
                if user != expect_user:
                    return httpx.Response(401)
            assert request.url.params["scope"] == "repository:o/i:pull"
            return httpx.Response(200, json={"token": "tok"})
        if request.headers.get("Authorization") != "Bearer tok":
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="https://auth.test/token",service="reg"'
                },
            )
        assert "application/vnd.oci.image.index.v1+json" in request.headers["Accept"]
        return httpx.Response(200, headers={"Docker-Content-Digest": DIGEST})

    return httpx.MockTransport(handler)


async def test_a_digest_is_resolved_through_a_bearer_challenge() -> None:
    client = RegistryV2Client(_bearer_registry("reader"), guard=False)
    digest = await client.resolve_digest(
        "reg.test/o/i:1", insecure=False, auth=RegistryAuth("reader", "pw")
    )
    assert digest == DIGEST


async def test_a_refused_credential_is_a_registry_error_without_the_secret() -> None:
    client = RegistryV2Client(_bearer_registry("reader"), guard=False)
    with pytest.raises(RegistryError) as exc:
        await client.resolve_digest(
            "reg.test/o/i:1", insecure=False, auth=RegistryAuth("intruder", "s3cret-pw")
        )
    assert "s3cret-pw" not in str(exc.value)


async def test_a_missing_image_says_so() -> None:
    client = RegistryV2Client(httpx.MockTransport(lambda r: httpx.Response(404)), guard=False)
    with pytest.raises(RegistryError, match="does not exist"):
        await client.resolve_digest("reg.test/o/i:1", insecure=True, auth=None)


async def test_a_registry_that_answers_a_different_digest_is_not_believed() -> None:
    other = "sha256:" + "d" * 64
    client = RegistryV2Client(
        httpx.MockTransport(
            lambda r: httpx.Response(200, headers={"Docker-Content-Digest": other})
        ),
        guard=False,
    )
    with pytest.raises(RegistryError, match="different digest"):
        await client.resolve_digest(f"reg.test/o/i@{DIGEST}", insecure=True, auth=None)


async def test_probe_reports_rather_than_raises() -> None:
    client = RegistryV2Client(httpx.MockTransport(lambda r: httpx.Response(200)), guard=False)
    result = await client.probe("reg.test", insecure=True, auth=None)
    assert result.reachable and result.authenticated is None


def test_metadata_and_link_local_addresses_are_refused() -> None:
    """Private and loopback stay allowed: a LAN or localhost registry is the normal case."""
    for blocked in ("169.254.169.254", "fe80::1", "0.0.0.0", "fd00:ec2::254"):
        with pytest.raises(BlockedAddressError):
            check_ip(blocked)
    for allowed in ("127.0.0.1", "192.168.8.50", "10.0.0.5"):
        check_ip(allowed)
