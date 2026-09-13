"""PreviewProvider port: long-lived containers that serve a built artifact over HTTP.

``ExecEnvProvider`` runs work to completion; this port is its opposite -- it starts
something that *stays up* and answers requests, so a human can open what the agents
built and try it. One preview is one container serving one repo's latest build
artifact on a fixed port.

Adapters report an **internal** address only (a container-name alias, a pod IP, a task
private IP). Nothing is published to the outside world: the API proxies previews at
``/p/{token}`` and stamps the security headers there, which is what lets one URL scheme
and one auth story cover socket / kubernetes / aws-ecs deployments without per-preview
ingress, DNS, or load-balancer rules.

``NullPreviewProvider`` is the disabled state -- callers treat
``PreviewUnavailableError`` as "this deployment cannot host previews", never as a fault,
matching the ``ExecEnvUnavailableError`` convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# Every preview container serves here; the port is an internal detail the proxy hides.
PREVIEW_PORT = 8080


class PreviewUnavailableError(Exception):
    """No preview backend is configured/reachable in this deployment."""


@dataclass(frozen=True)
class PreviewHandle:
    """``ref`` is the engine-side handle teardown/status use (container name, job name,
    task ARN). ``internal_url`` is reachable from the api process only."""

    ref: str
    internal_url: str


class PreviewProvider(Protocol):
    async def start(
        self,
        name: str,
        image: str,
        command: str,
        *,
        env: dict[str, str],
        port: int = PREVIEW_PORT,
        ttl_seconds: int | None = None,
        registry_auth: str | None = None,
    ) -> PreviewHandle:
        """Start (or reuse -- names are deterministic, so this converges instead of
        leaking a second container) a long-lived container running ``command``, and
        return once it has an address. Never waits for the command to finish.

        ``ttl_seconds`` is a hint: engines that can enforce expiry themselves should,
        so a preview still dies when the worker is down. Engines that cannot ignore it
        and rely on the worker's reaper."""
        ...

    async def status(self, ref: str) -> str:
        """``running`` | ``stopped`` | ``failed`` | ``missing``."""
        ...

    async def teardown(self, ref: str) -> None: ...

    async def teardown_matching(self, prefix: str) -> int:
        """Tear down every preview whose name starts with ``prefix``. Returns the count."""
        ...


class NullPreviewProvider:
    async def start(
        self,
        name: str,
        image: str,
        command: str,
        *,
        env: dict[str, str],
        port: int = PREVIEW_PORT,
        ttl_seconds: int | None = None,
        registry_auth: str | None = None,
    ) -> PreviewHandle:
        raise PreviewUnavailableError("no preview backend is configured")

    async def status(self, ref: str) -> str:
        return "missing"

    async def teardown(self, ref: str) -> None:
        return None

    async def teardown_matching(self, prefix: str) -> int:
        return 0
