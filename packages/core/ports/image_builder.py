"""ImageBuilder port: hand a Dockerfile to a builder the operator chose, and follow it.

Pyrrhula has no builder of its own. A tenant's RUN steps execute on infrastructure the
operator governs -- a CI system, a webhook in front of a build farm, a Docker engine behind
Portainer -- and the platform's part is to say what to build and where to push it, follow
progress, and then *not believe the result*: ``core.images.service`` asks the registry
for the digest and smoke-tests it before anything runs in it.

Two shapes, because external systems come in two:

- ``poll``: ``submit`` returns at once with an external id; ``poll`` is asked later. CI
  systems and webhooks. The worker is never held for the build's duration.
- ``stream``: ``run`` holds a connection for the whole build (a Docker engine's build
  endpoint). Implemented by the Portainer adapter in a later phase.

Inputs are inert by construction: the Dockerfile travels base64-encoded or as a JSON
string, never interpolated into anything the builder executes as shell, and every name
and reference is restricted to a charset that cannot carry a command. Credentials are
held by the adapter (``repr=False``) and never appear in a ``Progress`` -- adapters run
every message they return through ``adapters.image_builder.redact``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

BuildState = Literal["queued", "building", "succeeded", "failed", "cancelled"]


class BuilderError(Exception):
    """The builder could not be reached or refused the request. Message is safe to show."""


@dataclass(frozen=True)
class BuildSpec:
    build_id: str
    dockerfile: str
    # Computed by Pyrrhula from the declared registry and the tenant's namespace; a
    # tenant never chooses where its image is pushed.
    target_ref: str
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Submitted:
    external_ref: str
    external_url: str = ""


@dataclass(frozen=True)
class Progress:
    state: BuildState
    digest: str = ""
    log_tail: str = ""
    error: str = ""
    external_url: str = ""


@dataclass(frozen=True)
class BuilderProbe:
    ok: bool
    detail: str


class ImageBuilder(Protocol):
    mode: Literal["poll", "stream"]

    async def submit(self, spec: BuildSpec) -> Submitted: ...

    async def poll(self, external_ref: str) -> Progress: ...

    async def cancel(self, external_ref: str) -> None: ...

    async def probe(self) -> BuilderProbe: ...


class StreamImageBuilder(Protocol):
    """A builder that holds a connection for the whole build (``mode == "stream"``).

    ``run`` builds and pushes, calling ``on_log`` with scrubbed output as it arrives and
    asking ``should_cancel`` between lines; returning ends the build in a terminal state
    (``succeeded`` with the pushed digest, ``failed`` or ``cancelled``). Closing the
    connection is what cancels the build on the engine.
    """

    mode: Literal["poll", "stream"]

    async def run(
        self,
        spec: BuildSpec,
        *,
        on_log: Callable[[str], Awaitable[None]],
        should_cancel: Callable[[], Awaitable[bool]],
        timeout_seconds: int,
    ) -> Progress: ...

    async def probe(self) -> BuilderProbe: ...


class NullImageBuilder:
    """No builder is configured: the UI disables Build and says why."""

    mode: Literal["poll", "stream"] = "poll"

    async def submit(self, spec: BuildSpec) -> Submitted:
        raise BuilderError("no image builder is configured for this organization")

    async def poll(self, external_ref: str) -> Progress:
        return Progress("failed", error="no image builder is configured")

    async def cancel(self, external_ref: str) -> None:
        return None

    async def probe(self) -> BuilderProbe:
        return BuilderProbe(False, "no builder configured")
