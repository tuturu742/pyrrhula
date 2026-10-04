"""Deciding whether a repo's registry credential may accompany a pull, and in what form.

A repo can carry a registry credential so that a private runtime image can be pulled. The
image a delegation actually runs, though, is chosen by layers -- the repo row, the
repository's own ``pyrrhula-build.json``, the runtime catalog -- and the middle one is a
file anyone with commit access can edit. The credential used to be sent to
``image.split("/")[0]`` of whatever came out, which let a commit point the image at a host
of its choosing and receive the password.

So the credential now records the registry it was issued for, and travels only to that
registry. Pure functions, so the decision can be tested without a container engine.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from core.repos.image_ref import registry_host, serveraddress_for

# Where the image came from, as ``BuildRecipe.sources["image"]`` spells it. Only an image
# the operator typed into the repo row is trusted for a credential that predates host
# binding, because no repository file can have chosen it.
OPERATOR_SOURCES = frozenset({"repo"})
_HUB = frozenset(
    {"docker.io", "index.docker.io", "registry-1.docker.io", "https://index.docker.io/v1/"}
)


def _host(value: str) -> str:
    return "docker.io" if value in _HUB else value


def credential_applies(credential: dict[str, Any], image: str, *, image_source: str | None) -> bool:
    """Whether this credential may be offered when pulling ``image``."""
    if not image:
        return False
    target = registry_host(image)
    bound = str(credential.get("serveraddress") or "").strip().lower()
    if bound:
        # Stored as registry_host() output; normalise Docker Hub's aliases the same way.
        return _host(bound) == target
    # A credential sealed before host binding existed says nothing about where it belongs.
    # Offer it only for an operator-typed image, never for one a repository file chose.
    return image_source in OPERATOR_SOURCES


def x_registry_auth(
    credential: dict[str, Any], image: str, *, image_source: str | None
) -> str | None:
    """The base64 ``X-Registry-Auth`` header value, or None when it must not be sent."""
    if not credential_applies(credential, image, image_source=image_source):
        return None
    payload = {
        "username": str(credential.get("username") or ""),
        "password": str(credential.get("password") or ""),
        "serveraddress": serveraddress_for(registry_host(image)),
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()
