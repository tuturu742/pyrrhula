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
    return ref
