"""``StreamImageBuilder`` over a Docker engine the operator governs through Portainer.

Portainer proxies the Docker Engine API at ``/api/endpoints/<id>/docker/…`` behind its own
API keys and access control, so a builder here is "a remote Docker engine the admin
manages": Pyrrhula holds a Portainer API key for a user that may use one environment, and
nothing on the engine host.

One build is three calls:

1. ``POST …/docker/build`` with a tar holding **only the Dockerfile** -- the build context
   is empty, so a RUN step sees nothing Pyrrhula did not put in the file. Memory, CPU and
   an optional network mode are set per build; ``pull=1`` refreshes the base. The response
   streams for the whole build; closing it cancels the build on the engine.
2. ``POST …/docker/images/<repo>/push?tag=…`` with ``X-Registry-Auth`` carrying the
   builder's **push** credential -- sent on this request only, never stored with a build.
   The push stream ends with the digest the registry accepted.
3. ``DELETE …/docker/images/<tag>`` so built images do not pile up on the engine.

``push_host`` covers a registry the engine reaches under another name -- typically one
running next to the engine, pushed to over loopback (which Docker trusts without TLS) and
pulled by everything else at its LAN address. Only the host differs; the repository, tag
and digest are the same, which is what Pyrrhula then verifies at the pull address.

The tenant's RUN steps execute on this engine. What they can reach is the operator's
decision, made visible by ``isolation_probe`` and recorded as an acknowledgement before
the builder can be used.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import re
import tarfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from adapters.image_builder.redact import redact
from core.net_guard import BlockedAddressError, check_host
from core.ports.image_builder import BuilderError, BuilderProbe, BuildSpec, Progress, Submitted

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_REF = re.compile(r"^[a-z0-9.:/_-]+:[A-Za-z0-9._-]+$")
# No output for this long means the engine is stuck, whatever the wall clock says.
_IDLE_SECONDS = 600.0
_CANCEL_CHECK_SECONDS = 5.0

# Run as a build so it sees exactly what a tenant's RUN step sees.
PROBE_DOCKERFILE = r"""FROM alpine:3.20
RUN set +e; echo "PROBE uid=$(id -u)"; \
 for t in http://169.254.169.254/latest/meta-data/ http://172.17.0.1:2375/version \
          http://172.17.0.1:2376/version https://ghcr.io/v2/ https://registry-1.docker.io/v2/ \
          __EXTRA__ ; do \
   code=$(wget -q -S -T 4 --no-check-certificate -O /dev/null "$t" 2>&1 \
          | awk '/HTTP\//{print $2; exit}'); \
   echo "PROBE reach $t -> ${code:-unreachable}"; \
 done; \
 echo "PROBE docker.sock: $(ls /var/run/docker.sock 2>&1)"; true
"""


@dataclass(frozen=True)
class PortainerConfig:
    base_url: str
    endpoint_id: int
    push_host: str = ""
    tls_verify: bool = True
    network_mode: str = ""
    memory_mb: int = 4096
    cpus: float = 2.0


def _tar(dockerfile: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = dockerfile.encode()
        info = tarfile.TarInfo("Dockerfile")
        info.size = len(data)
        info.mtime = 0
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def push_ref(target_ref: str, push_host: str) -> str:
    """``target_ref`` with its registry host swapped for the one the engine pushes to."""
    if not push_host:
        return target_ref
    _host, _, rest = target_ref.partition("/")
    return f"{push_host}/{rest}"


@dataclass
class PortainerImageBuilder:
    config: PortainerConfig
    api_key: str = field(repr=False)
    push_username: str = field(default="", repr=False)
    push_password: str = field(default="", repr=False)
    transport: httpx.AsyncBaseTransport | None = None
    guard: bool = True
    mode: Literal["poll", "stream"] = "stream"

    def _secrets(self) -> list[str]:
        return [self.api_key, self.push_password]

    def _docker(self, path: str) -> str:
        return f"/api/endpoints/{int(self.config.endpoint_id)}/docker{path}"

    async def _client(self, timeout: httpx.Timeout | float) -> httpx.AsyncClient:
        parsed = httpx.URL(self.config.base_url)
        if parsed.scheme != "https":
            raise BuilderError("the Portainer URL must be https")
        if self.guard:
            try:
                await check_host(parsed.host)
            except BlockedAddressError as exc:
                raise BuilderError(str(exc)) from exc
            except OSError as exc:
                raise BuilderError(f"cannot resolve {parsed.host}") from exc
        return httpx.AsyncClient(
            base_url=self.config.base_url.rstrip("/"),
            headers={"X-API-Key": self.api_key},
            transport=self.transport,
            verify=self.config.tls_verify,
            timeout=timeout,
            follow_redirects=False,
        )

    def _registry_auth(self, ref: str) -> str:
        host = ref.split("/", 1)[0]
        payload = {
            "username": self.push_username,
            "password": self.push_password,
            "serveraddress": host,
        }
        return base64.b64encode(json.dumps(payload).encode()).decode()

    async def _stream(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        params: dict[str, str],
        content: bytes | None,
        headers: dict[str, str],
        on_log: Callable[[str], Awaitable[None]],
        should_cancel: Callable[[], Awaitable[bool]],
        deadline: float,
    ) -> tuple[str, str, list[dict[str, Any]]]:
        """Read a Docker JSON stream to the end. Returns (outcome, message, aux records),
        outcome one of ok | error | cancelled | timeout."""
        aux: list[dict[str, Any]] = []
        last_check = 0.0
        try:
            async with client.stream(
                method, path, params=params, content=content, headers=headers
            ) as resp:
                if resp.status_code >= 300:
                    body = (await resp.aread()).decode(errors="replace")
                    return "error", f"HTTP {resp.status_code}: {body[:300]}", aux
                async for line in resp.aiter_lines():
                    now = time.monotonic()
                    if now > deadline:
                        return "timeout", "", aux
                    if now - last_check > _CANCEL_CHECK_SECONDS:
                        last_check = now
                        if await should_cancel():
                            return "cancelled", "", aux
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if obj.get("error") or obj.get("errorDetail"):
                        detail = obj.get("error") or (obj.get("errorDetail") or {}).get("message")
                        return "error", str(detail), aux
                    if isinstance(obj.get("aux"), dict):
                        aux.append(obj["aux"])
                    if obj.get("stream"):
                        text = str(obj["stream"])
                    elif obj.get("status") and not obj.get("id"):
                        # Per-layer status ("Preparing", "Pushed" for each id) is noise;
                        # the lines without an id are the ones a person reads.
                        text = f"{obj['status']}\n"
                    else:
                        continue
                    await on_log(redact(text, self._secrets(), limit=0))
        except httpx.ReadTimeout:
            return "timeout", "the engine went silent", aux
        except httpx.HTTPError as exc:
            return "error", f"Portainer is unreachable ({type(exc).__name__})", aux
        return "ok", "", aux

    async def run(
        self,
        spec: BuildSpec,
        *,
        on_log: Callable[[str], Awaitable[None]],
        should_cancel: Callable[[], Awaitable[bool]],
        timeout_seconds: int,
    ) -> Progress:
        if not _REF.match(spec.target_ref):
            raise BuilderError("refusing a target outside the safe charset")
        tag = push_ref(spec.target_ref, self.config.push_host)
        repository, _, tag_name = tag.rpartition(":")
        deadline = time.monotonic() + timeout_seconds
        log: list[str] = []

        async def collect(text: str) -> None:
            log.append(text)
            if len(log) > 400:  # only the tail is ever kept
                del log[:200]
            await on_log(text)

        def tail() -> str:
            return redact("".join(log), self._secrets())

        params: dict[str, str] = {
            "t": tag,
            "rm": "1",
            "forcerm": "1",
            "pull": "1",
            "labels": json.dumps(spec.labels),
            "memory": str(self.config.memory_mb * 2**20),
            "memswap": str(self.config.memory_mb * 2**20),
            "cpuperiod": "100000",
            "cpuquota": str(int(self.config.cpus * 100000)),
        }
        if self.config.network_mode:
            params["networkmode"] = self.config.network_mode

        timeout = httpx.Timeout(30.0, read=_IDLE_SECONDS)
        async with await self._client(timeout) as client:
            outcome, message, _ = await self._stream(
                client,
                "POST",
                self._docker("/build"),
                params=params,
                content=_tar(spec.dockerfile),
                headers={"Content-Type": "application/x-tar"},
                on_log=collect,
                should_cancel=should_cancel,
                deadline=deadline,
            )
            if outcome != "ok":
                return self._ended(outcome, f"the build failed: {message}", tail())

            outcome, message, aux = await self._stream(
                client,
                "POST",
                self._docker(f"/images/{repository}/push"),
                params={"tag": tag_name},
                content=None,
                headers={"X-Registry-Auth": self._registry_auth(tag)},
                on_log=collect,
                should_cancel=should_cancel,
                deadline=deadline,
            )
            # The image is in the registry or it is not; either way the engine's copy of
            # this tag has done its job.
            with contextlib.suppress(httpx.HTTPError):
                await client.delete(self._docker(f"/images/{tag}"), params={"noprune": "0"})
            if outcome != "ok":
                return self._ended(outcome, f"the push failed: {message}", tail())

        digest = next(
            (str(a["Digest"]) for a in reversed(aux) if _DIGEST.fullmatch(str(a.get("Digest")))),
            "",
        )
        return Progress("succeeded", digest=digest, log_tail=tail())

    def _ended(self, outcome: str, message: str, log_tail: str) -> Progress:
        if outcome == "cancelled":
            return Progress("cancelled", log_tail=log_tail)
        if outcome == "timeout":
            return Progress("failed", error="the build ran out of time", log_tail=log_tail)
        return Progress(
            "failed", error=redact(message, self._secrets(), limit=1000), log_tail=log_tail
        )

    async def probe(self) -> BuilderProbe:
        try:
            async with await self._client(30.0) as client:
                resp = await client.get(self._docker("/info"))
        except BuilderError as exc:
            return BuilderProbe(False, str(exc))
        except httpx.HTTPError as exc:
            return BuilderProbe(False, f"Portainer is unreachable ({type(exc).__name__})")
        if resp.status_code != 200:
            return BuilderProbe(False, f"Portainer answered HTTP {resp.status_code}")
        info = resp.json()
        return BuilderProbe(
            True,
            f"Docker {info.get('ServerVersion')} on {info.get('OperatingSystem')}, "
            f"{info.get('NCPU')} CPUs",
        )

    async def isolation_probe(self, extra_targets: list[str]) -> list[str]:
        """Build a throwaway image whose RUN step reports what it can reach -- what any
        tenant's RUN step on this engine can reach. Nothing is pushed."""
        extras = " ".join(
            t for t in extra_targets if re.fullmatch(r"https?://[A-Za-z0-9.:/_-]+", t)
        )
        dockerfile = PROBE_DOCKERFILE.replace("__EXTRA__", extras)
        lines: list[str] = []

        async def collect(text: str) -> None:
            lines.extend(x.strip() for x in text.splitlines() if x.strip().startswith("PROBE"))

        async def never() -> bool:
            return False

        tag = "pyrrhula-isolation-probe:latest"
        async with await self._client(httpx.Timeout(30.0, read=300.0)) as client:
            params = {"t": tag, "rm": "1", "forcerm": "1", "nocache": "1", "pull": "1"}
            if self.config.network_mode:
                params["networkmode"] = self.config.network_mode
            outcome, message, _ = await self._stream(
                client,
                "POST",
                self._docker("/build"),
                params=params,
                content=_tar(dockerfile),
                headers={"Content-Type": "application/x-tar"},
                on_log=collect,
                should_cancel=never,
                deadline=time.monotonic() + 300,
            )
            with contextlib.suppress(httpx.HTTPError):
                await client.delete(self._docker(f"/images/{tag}"), params={"force": "1"})
        if outcome != "ok":
            lines.append(f"PROBE build did not finish: {outcome} {message}")
        return [line.removeprefix("PROBE ") for line in lines]

    # The poll half of the port does not apply to a stream builder.
    async def submit(self, spec: BuildSpec) -> Submitted:
        raise BuilderError("a Portainer build is run, not submitted")

    async def poll(self, external_ref: str) -> Progress:
        raise BuilderError("a Portainer build is run, not polled")

    async def cancel(self, external_ref: str) -> None:
        return None
