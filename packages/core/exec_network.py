"""What a delegated environment may reach.

Deny-by-default is not "no network". A delegation *must* reach Pyrrhula -- it clones the
hosted store over git smart-HTTP and, with a harness, calls models through the inference
proxy -- and it very often must reach a package registry, because changing what a project
depends on is ordinary work. "Migrate this to Java 21" is a dependency change before it is
anything else, and an agent that cannot fetch the new artifacts cannot do it. Baking
dependencies at build time does not answer that: the need appears mid-run, after the agent
has decided.

So the control is an **allowlist**, configurable per engine, and two rules shape it:

*Pyrrhula is never on it.* The api is reached directly and always -- it is the one
destination the delegation cannot work without, and routing it through a proxy would put
the git job token and the inference token in front of something that does not need to see
them. It goes in ``no_proxy``.

*Everything else goes through the proxy, or nowhere.* An allowlist only means anything if
the container has no second route. Setting proxy variables in a container that can still
reach the internet directly is not a control, it is a suggestion -- an agent with a shell
can ignore an environment variable. That is why ``mode`` defaults to ``open``: a
deployment is honestly unrestricted until an operator has put an internal network and a
proxy in place, and pretending otherwise would be worse than not offering the feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

# Registries a coding task reaches for as a matter of course. Offered as a starting point
# an operator can name rather than a default anyone is given: the point of an allowlist is
# that somebody chose what is on it.
COMMON_PACKAGE_HOSTS = (
    "registry.npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
    "crates.io",
    "static.crates.io",
    "proxy.golang.org",
    "repo.maven.apache.org",
    "deb.debian.org",
    "security.debian.org",
)


@dataclass(frozen=True)
class EgressPlan:
    """How one environment's network is configured."""

    mode: str = "open"
    proxy: str = ""
    allow: tuple[str, ...] = ()
    direct: tuple[str, ...] = field(default_factory=tuple)

    @property
    def restricted(self) -> bool:
        return self.mode == "proxied" and bool(self.proxy)

    def env(self) -> dict[str, str]:
        """The proxy variables to export in the container.

        Both cases of each name: ``curl`` reads the lowercase form, most language
        toolchains read the uppercase one, and a container that honours only one of them
        is a container where half the traffic silently takes the other route.
        """
        if not self.restricted:
            return {}
        skip = ",".join(self.direct) if self.direct else ""
        out = {
            "HTTP_PROXY": self.proxy,
            "HTTPS_PROXY": self.proxy,
            "http_proxy": self.proxy,
            "https_proxy": self.proxy,
        }
        if skip:
            out["NO_PROXY"] = skip
            out["no_proxy"] = skip
        return out


def _host_of(url: str) -> str:
    """The bare host a base URL names -- **no port**.

    Measured, not assumed: with ``no_proxy=192.168.8.241:8100`` curl ignored the entry
    entirely and sent the request to the proxy, which refused it as an unlisted domain.
    A delegation on a restricted deployment would have failed at its git clone, with the
    proxy log as the only clue. curl and git match ``no_proxy`` on host alone, and those
    two are precisely what the work script uses.

    Exempting every port on our own api rather than one is the right side to err on: it is
    our host, and the alternative is an entry that matches nothing.
    """
    if not url:
        return ""
    parts = urlsplit(url if "//" in url else f"//{url}")
    netloc = parts.netloc or parts.path.split("/")[0]
    if not netloc:
        return ""
    # An IPv6 literal keeps its brackets; anything else splits on the last colon.
    if netloc.startswith("["):
        return netloc.split("]")[0] + "]"
    return netloc.rsplit(":", 1)[0] if ":" in netloc else netloc


def plan_for(engine: dict[str, Any] | None, *, api_base: str = "") -> EgressPlan:
    """The egress plan for one engine.

    ``api_base`` is the route the container uses to reach Pyrrhula -- the same value the
    work script clones from -- and is added to ``no_proxy`` automatically. An operator
    should not have to remember to exempt the one host the delegation cannot work without,
    and forgetting it would send a job token through a proxy for no reason.
    """
    declared = engine or {}
    mode = str(declared.get("egress_mode") or "open").strip().lower()
    proxy = str(declared.get("egress_proxy") or "").strip()

    raw_allow = declared.get("egress_allow")
    allow = tuple(str(h).strip() for h in raw_allow if str(h).strip()) if raw_allow else ()

    direct = [host for host in (_host_of(api_base),) if host]
    for extra in declared.get("egress_direct") or []:
        host = str(extra).strip()
        if host and host not in direct:
            direct.append(host)

    return EgressPlan(mode=mode, proxy=proxy, allow=allow, direct=tuple(direct))
