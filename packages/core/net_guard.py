"""Refusing outbound requests to addresses that only an attacker would want reached.

Some hosts Pyrrhula contacts are typed by an operator -- a registry, later a build
service or a webhook. Operators are trusted, but a typo, a hostile DNS answer, or an
autocompleted value can point a server-side request at the cloud metadata endpoint, which
hands out the instance's own credentials to anything that asks from inside.

Private and loopback addresses are **allowed** on purpose: a registry on the LAN or on
``localhost:5000`` is the normal self-hosted case. What is refused is the set no
legitimate registry or build service lives at: link-local (where metadata services sit),
the unspecified address, and multicast.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

_METADATA_V6 = ipaddress.ip_address("fd00:ec2::254")


class BlockedAddressError(ValueError):
    pass


def check_ip(value: str) -> None:
    ip = ipaddress.ip_address(value)
    if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip == _METADATA_V6:
        raise BlockedAddressError(
            f"{value} is a link-local, unspecified or multicast address -- refusing to "
            "contact it (cloud metadata services live there)"
        )


async def check_host(host: str) -> None:
    """Resolve ``host`` (no port) and refuse if any address it resolves to is blocked.

    Every address is checked, not just the first: a name that resolves to one harmless
    and one metadata address would otherwise depend on which one the client picked.
    """
    host = host.strip("[]")
    try:
        check_ip(host)
        return
    except ValueError as exc:
        if isinstance(exc, BlockedAddressError):
            raise
    infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    for info in infos:
        check_ip(str(info[4][0]))
