"""Port: talking to a container registry over the Docker Registry HTTP API v2.

Pyrrhula never trusts a builder's word for what it pushed, nor a bundle's for what it
points at. It asks the registry: does this reference exist, and what is its digest. That
is all this port does -- reading, never pushing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class RegistryError(Exception):
    """The registry could not answer, or answered no. The message is safe to show: it
    never contains a credential."""


@dataclass(frozen=True)
class RegistryAuth:
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class ProbeResult:
    reachable: bool
    authenticated: bool | None  # None: the registry asked for nothing
    detail: str


class RegistryClient(Protocol):
    async def probe(self, host: str, *, insecure: bool, auth: RegistryAuth | None) -> ProbeResult:
        """Is the registry there, and does the credential (if any) work?"""
        ...

    async def resolve_digest(self, ref: str, *, insecure: bool, auth: RegistryAuth | None) -> str:
        """``sha256:…`` for ``ref`` (tag or digest). Raises ``RegistryError``."""
        ...
