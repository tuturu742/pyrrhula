"""Validating a container image reference before it reaches a container engine.

A repo's `runtime_image` (and a preview's `preview_image`) were stored verbatim, so
anything at all could be saved and the first sign of trouble was a pull failure inside a
job nobody was watching. The common mistake is not exotic: you find the image on Docker
Hub and paste the address bar, which yields
`https://hub.docker.com/r/barichello/godot-ci/` -- a web page, not an image. The engine
cannot pull it, the preflight that checks the image contains git never runs because there
is no container, and the delegation fails somewhere far away from the field that caused it.

This refuses that at the point it is typed, and says what the reference should have been.
It validates *shape*, not existence: whether a registry actually serves the image is the
engine's question to answer, and guessing at it here would only add a second place to be
wrong about it.
"""

from __future__ import annotations

import re

_REGISTRY_PAGE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Docker Hub's own two shapes: /r/<org>/<name> for user images, /_/<name> for official.
    (re.compile(r"^https?://hub\.docker\.com/r/([^/?#]+/[^/?#]+)"), r"\1"),
    (re.compile(r"^https?://hub\.docker\.com/_/([^/?#]+)"), r"\1"),
    (re.compile(r"^https?://ghcr\.io/([^?#]+)"), r"ghcr.io/\1"),
    (re.compile(r"^https?://quay\.io/repository/([^?#]+)"), r"quay.io/\1"),
    (re.compile(r"^https?://gallery\.ecr\.aws/([^?#]+)"), r"public.ecr.aws/\1"),
)

# Deliberately permissive: registry hosts carry ports, names carry slashes, and a
# reference may end in a :tag or an @sha256: digest. Anything with whitespace or a
# scheme is not a reference, and that is most of what actually gets pasted in.
_REF = re.compile(r"^[A-Za-z0-9._\-/:@]+$")


class ImageRefError(ValueError):
    """A value that no container engine could pull. Always suggests the fix."""


def normalise_image_ref(value: str | None) -> str | None:
    """Return a usable image reference, or raise explaining what was wrong.

    Empty is legitimate (it means "use the catalog default"), so it passes through.
    """
    if value is None:
        return None
    ref = value.strip()
    if not ref:
        return None

    for pattern, replacement in _REGISTRY_PAGE_PATTERNS:
        match = pattern.match(ref)
        if match:
            suggestion = match.expand(replacement).rstrip("/")
            raise ImageRefError(
                f"that is the registry's web page, not an image reference. Use "
                f"'{suggestion}:<tag>' -- the tag matters, and 'latest' is often not "
                f"the one you want."
            )

    if "://" in ref:
        raise ImageRefError(
            "an image reference has no scheme: drop the 'https://' and use "
            "'<registry>/<org>/<name>:<tag>' (or just '<org>/<name>:<tag>' for Docker Hub)"
        )
    if any(ch.isspace() for ch in ref):
        raise ImageRefError("an image reference cannot contain spaces")
    if not _REF.match(ref):
        raise ImageRefError(
            f"{ref!r} is not a usable image reference; expected "
            "'<registry>/<org>/<name>:<tag>' or '<org>/<name>:<tag>'"
        )
    if ref.endswith("/"):
        raise ImageRefError("an image reference does not end in '/'")
    if not has_tag_or_digest(ref):
        # Not pedantry. Asked for an image with no tag, the engine's pull endpoint pulls
        # *every* tag of the repository, and which one then runs is whatever the engine
        # resolves the bare name to. Say which one is meant.
        raise ImageRefError(
            f"{ref!r} has no tag: add ':<tag>' (or '@sha256:<digest>'). An untagged "
            "reference makes the engine pull every tag of the repository"
        )
    return ref


# ── parsing, by Docker's own rules ─────────────────────────────────────────────────────
#
# A registry credential is only safe to send to the registry it was issued for, and the
# image a delegation runs can come from a file inside the repository. So "which registry
# does this reference name" has to be answered exactly the way the engine answers it --
# a guess like `image.split("/")[0]` sent Docker Hub credentials to a host called
# `barichello` and let a manifest choose where a repo's credential went.

_DOCKER_HUB = "docker.io"
_HUB_ALIASES = frozenset({"docker.io", "index.docker.io", "registry-1.docker.io"})
_DIGEST = re.compile(r"@[A-Za-z0-9_+.-]+:[0-9a-fA-F]+$")
_PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")


def _split_domain(ref: str) -> tuple[str | None, str]:
    """``(registry host or None, remainder)`` by Docker's rule: the first path component is
    a registry only if there is a second one and it looks like a host -- it contains a
    ``.`` or a ``:``, or is exactly ``localhost``."""
    first, sep, rest = ref.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        return first.lower(), rest
    return None, ref


def registry_host(ref: str) -> str:
    """The registry a reference pulls from, canonical for Docker Hub's several names."""
    host, _ = _split_domain(ref.strip())
    if host is None or host in _HUB_ALIASES:
        return _DOCKER_HUB
    return host


def has_tag_or_digest(ref: str) -> bool:
    """Whether the reference names one image rather than a whole repository."""
    if _DIGEST.search(ref):
        return True
    _, remainder = _split_domain(ref)
    # A ':' after the last '/' is a tag; one before it belongs to the registry's port.
    return ":" in remainder.rsplit("/", 1)[-1]


def is_digest_pinned(ref: str) -> bool:
    """``…@sha256:<64 hex>`` -- the only form that cannot change underneath a reader."""
    return bool(_PINNED.search(ref.strip()))


def canonical(ref: str) -> str:
    """The fully qualified spelling: ``python:3.12`` -> ``docker.io/library/python:3.12``.

    Two spellings of one image must compare equal wherever a reference is checked against
    a policy, or the policy has a bypass that is just a different way of typing it.
    """
    ref = ref.strip()
    host, remainder = _split_domain(ref)
    host = _DOCKER_HUB if host is None or host in _HUB_ALIASES else host
    if host == _DOCKER_HUB and "/" not in remainder.split("@", 1)[0].rsplit(":", 1)[0]:
        remainder = f"library/{remainder}"
    return f"{host}/{remainder}"


def serveraddress_for(host: str) -> str:
    """What a Docker ``X-Registry-Auth`` header's ``serveraddress`` must say for a host.

    Docker Hub's credential is keyed by its historical index URL, not by ``docker.io``;
    every other registry by ``host[:port]``.
    """
    host = host.strip().lower()
    if host in _HUB_ALIASES:
        return "https://index.docker.io/v1/"
    return host
