"""``RegistryClient`` over the Docker Registry HTTP API v2 -- read-only.

Authentication follows the registry's own challenge: ``GET /v2/`` answers 401 with a
``WWW-Authenticate`` header saying either ``Basic`` (send the credential directly) or
``Bearer realm=…,service=…`` (fetch a short-lived token from the realm, scoped to the
repository being read). That covers registry:2 with htpasswd, Docker Hub, GHCR, Harbor and
most others without per-registry code.

A digest comes from a ``HEAD`` on the manifest (``Docker-Content-Digest``), asking for
every manifest type a modern image might be -- OCI index, OCI manifest, Docker manifest
list, Docker v2 -- so a multi-arch image answers with its index digest, the one an engine
pulls by. Docker documents that HEAD manifest requests do not count against its rate limit.

Redirects are not followed and every host contacted -- the registry and the token realm --
is checked against ``core.net_guard`` first. Credentials appear in requests only, never in
an exception message.
"""

from __future__ import annotations

import base64
import re

import httpx

from core.net_guard import BlockedAddressError, check_host
from core.ports.image_registry import ProbeResult, RegistryAuth, RegistryError
from core.repos.image_ref import canonical

_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHALLENGE_PARAM = re.compile(r'(\w+)="([^"]*)"')
# Docker Hub's registry API is not served at docker.io.
_API_HOST = {"docker.io": "registry-1.docker.io"}


def split_ref(ref: str) -> tuple[str, str, str]:
    """``(host, repository, reference)`` for a canonical reference."""
    full = canonical(ref)
    host, _, rest = full.partition("/")
    if "@" in rest:
        repository, _, reference = rest.partition("@")
    else:
        repository, sep, reference = rest.rpartition(":")
        if not sep or "/" in reference:
            repository, reference = rest, "latest"
    return host, repository, reference


class RegistryV2Client:
    def __init__(
        self, transport: httpx.AsyncBaseTransport | None = None, *, guard: bool = True
    ) -> None:
        self._transport = transport
        # Off only in tests with a mock transport, where there is no address to resolve.
        self._guard = guard

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=20.0, follow_redirects=False)

    async def _checked(self, host: str) -> None:
        if not self._guard:
            return
        try:
            await check_host(host.rsplit(":", 1)[0] if host.count(":") == 1 else host)
        except BlockedAddressError as exc:
            raise RegistryError(str(exc)) from exc
        except OSError as exc:
            raise RegistryError(f"cannot resolve {host}: {exc.strerror or exc}") from exc

    def _base(self, host: str, insecure: bool) -> str:
        api = _API_HOST.get(host, host)
        return f"{'http' if insecure else 'https'}://{api}"

    async def _authorization(
        self,
        client: httpx.AsyncClient,
        challenge: str,
        scope: str | None,
        auth: RegistryAuth | None,
    ) -> str | None:
        """The ``Authorization`` header the challenge asks for, or None if none applies."""
        scheme, _, params_text = challenge.partition(" ")
        if scheme.lower() == "basic":
            if auth is None:
                return None
            pair = f"{auth.username}:{auth.password}".encode()
            return "Basic " + base64.b64encode(pair).decode()
        if scheme.lower() != "bearer":
            raise RegistryError(f"unsupported auth scheme {scheme!r}")
        params = dict(_CHALLENGE_PARAM.findall(params_text))
        realm = params.get("realm")
        if not realm:
            raise RegistryError("the registry asked for a token but named no realm")
        realm_url = httpx.URL(realm)
        await self._checked(realm_url.host)
        query = {"service": params.get("service", "")}
        if scope:
            query["scope"] = scope
        resp = await client.get(
            realm,
            params=query,
            auth=(auth.username, auth.password) if auth else None,
        )
        if resp.status_code in (401, 403):
            raise RegistryError("the registry refused the credential")
        if resp.status_code >= 400:
            raise RegistryError(f"token request failed: HTTP {resp.status_code}")
        body = resp.json()
        token = body.get("token") or body.get("access_token")
        if not token:
            raise RegistryError("the token service returned no token")
        return f"Bearer {token}"

    async def probe(self, host: str, *, insecure: bool, auth: RegistryAuth | None) -> ProbeResult:
        try:
            await self._checked(host)
            async with self._client() as client:
                base = self._base(host, insecure)
                resp = await client.get(f"{base}/v2/")
                if resp.status_code == 200:
                    return ProbeResult(True, None, "reachable; no authentication required")
                if resp.status_code != 401:
                    return ProbeResult(False, None, f"/v2/ answered HTTP {resp.status_code}")
                challenge = resp.headers.get("WWW-Authenticate", "")
                header = await self._authorization(client, challenge, None, auth)
                if header is None:
                    return ProbeResult(True, False, "reachable; requires a credential")
                again = await client.get(f"{base}/v2/", headers={"Authorization": header})
                ok = again.status_code == 200
                return ProbeResult(True, ok, "credential accepted" if ok else "credential refused")
        except RegistryError as exc:
            return ProbeResult(False, None, str(exc))
        except httpx.HTTPError as exc:
            return ProbeResult(False, None, f"unreachable ({type(exc).__name__})")

    async def resolve_digest(self, ref: str, *, insecure: bool, auth: RegistryAuth | None) -> str:
        host, repository, reference = split_ref(ref)
        await self._checked(host)
        url = f"{self._base(host, insecure)}/v2/{repository}/manifests/{reference}"
        headers = {"Accept": _ACCEPT}
        try:
            async with self._client() as client:
                resp = await client.head(url, headers=headers)
                if resp.status_code == 401:
                    challenge = resp.headers.get("WWW-Authenticate", "")
                    header = await self._authorization(
                        client, challenge, f"repository:{repository}:pull", auth
                    )
                    if header is None:
                        raise RegistryError(f"{host} requires a credential to read {repository}")
                    resp = await client.head(url, headers={**headers, "Authorization": header})
        except httpx.HTTPError as exc:
            raise RegistryError(f"{host} unreachable ({type(exc).__name__})") from exc
        if resp.status_code == 404:
            raise RegistryError(f"{repository}:{reference} does not exist on {host}")
        if resp.status_code >= 400:
            raise RegistryError(f"{host} answered HTTP {resp.status_code} for {repository}")
        digest = str(resp.headers.get("Docker-Content-Digest", ""))
        if not _DIGEST.match(digest):
            raise RegistryError(f"{host} returned no usable digest for {repository}")
        if reference.startswith("sha256:") and digest != reference:
            raise RegistryError("the registry returned a different digest than was asked for")
        return digest
