"""Pulling an image through a Docker-compatible engine socket -- one implementation.

The execution-environment adapter and the preview adapter each carried a copy of this,
and copies drift: a fix to one (an explicit tag, error scanning) has to be remembered in
the other. Both now call this.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from core.repos.image_ref import has_tag_or_digest

Request = Callable[..., Awaitable[httpx.Response]]


def pull_params(image: str) -> dict[str, str]:
    """Query parameters for ``POST /images/create``.

    An untagged ``fromImage`` makes the Docker engine pull **every** tag of the
    repository. Validation now refuses untagged references where they are typed, but rows
    stored before that still exist, so an explicit ``latest`` is sent for them -- which is
    what running a bare name meant anyway.
    """
    if has_tag_or_digest(image):
        return {"fromImage": image}
    return {"fromImage": image, "tag": "latest"}


async def ensure_image(
    request: Request,
    image: str,
    registry_auth: str | None,
    *,
    unavailable: Callable[[str], Exception],
    timeout: float = 600.0,
) -> None:
    """Pull ``image``; raise ``unavailable(message)`` when the engine refuses.

    ``X-Registry-Auth`` is the Docker-API convention for per-pull credentials (base64
    JSON); podman's compat API honours it too. The pull endpoint streams progress JSON,
    and an error mid-stream still returns 200 with an ``{"error": ...}`` line -- scanned
    for, or a failed pull would be reported as a success.
    """
    headers = {"X-Registry-Auth": registry_auth} if registry_auth else None
    resp = await request(
        "POST", "/images/create", params=pull_params(image), headers=headers, timeout=timeout
    )
    if resp.status_code >= 400:
        raise unavailable(f"could not pull {image!r}: {resp.text[:200]}")
    for line in resp.text.splitlines():
        try:
            obj: Any = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("error"):
            raise unavailable(f"pull {image!r} failed: {str(obj['error'])[:200]}")
