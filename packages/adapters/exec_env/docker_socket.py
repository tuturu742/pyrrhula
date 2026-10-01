"""v1 ``ExecEnvProvider``: sibling containers via the host container engine's
Docker-compatible REST API over a unix socket (rootless podman's ``podman.socket``).

Not docker-in-docker: the worker container mounts the *host's* socket, so environments are
ordinary sibling containers on the host -- on the engine's default network (internet for
package installs, no membership in the stack's networks), with the git-store volume mounted
so a clone needs no network at all. Spoken with plain httpx over the socket (no engine CLI
or SDK in the image).

Streams from ``exec`` are Docker's multiplexed frame format when Tty=false: an 8-byte
header (stream byte, 3 pad, u32 BE length) per frame -- parsed here, interleaved
stdout/stderr concatenated in arrival order.
"""

from __future__ import annotations

import hashlib
import json
import struct
from typing import Any

import httpx

from adapters.exec_env.engine_images import ensure_image
from adapters.exec_env.shell import shell_command
from core.exec_limits import ExecLimits, limits_for
from core.ports.exec_env import ExecEnvUnavailableError, ExecResult

_API = "http://d/v1.40"


class DockerSocketExecEnvProvider:
    def __init__(
        self,
        socket_path: str,
        *,
        network: str | None = None,
        limits: ExecLimits | None = None,
    ) -> None:
        # Optional engine-declared network for environments (e.g. a dedicated
        # 'pyrrhula-envs' network that carries the api -- for git smart-HTTP -- but NOT
        # the database). None = the engine's default network.
        self._network = network
        self._socket = socket_path
        # What this container may consume. With a coding harness the commands inside are
        # an agent's own choices, so "bounded" is what makes running them reasonable -- a
        # wall-clock timeout stops a long run, not a greedy one.
        self._limits = limits or limits_for(None)

    def _host_limits(self) -> dict[str, Any]:
        """The HostConfig fields that bound a container.

        A declared zero means the operator chose unlimited, so the key is omitted rather
        than sent as 0 -- which Docker reads as "no limit" for some fields and as an error
        for others.
        """
        limits = self._limits
        out: dict[str, Any] = {}
        if limits.memory_mb > 0:
            out["Memory"] = limits.memory_bytes
            # Without this the kernel may swap instead of refusing, which turns a memory
            # limit into a machine that thrashes rather than one that stops.
            out["MemorySwap"] = limits.memory_bytes
        if limits.cpus > 0:
            out["NanoCpus"] = limits.nano_cpus
        if limits.pids > 0:
            out["PidsLimit"] = limits.pids
        return out

    def _client(self, timeout: float = 600.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=self._socket), timeout=timeout
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 600.0,
    ) -> httpx.Response:
        try:
            async with self._client(timeout) as client:
                return await client.request(
                    method, f"{_API}{path}", json=json_body, params=params, headers=headers
                )
        except httpx.HTTPError as exc:
            raise ExecEnvUnavailableError(f"engine socket unreachable: {exc}") from exc

    async def _ensure_image(self, image: str, registry_auth: str | None = None) -> None:
        await ensure_image(self._request, image, registry_auth, unavailable=ExecEnvUnavailableError)

    def _identity_labels(self, image: str) -> dict[str, str]:
        """What a reusable container was created *for*.

        Environments are found again by name, and the name says which session and repo
        they belong to -- not which image or limits they run with. So a rebuilt image, or
        limits an operator tightened, were silently ignored for the rest of the session:
        the old container answered to the name. These labels let reuse notice.
        """
        limits = self._host_limits()
        digest = hashlib.sha256(json.dumps(limits, sort_keys=True).encode()).hexdigest()[:12]
        return {"pyrrhula.image": image, "pyrrhula.limits": digest}

    async def provision(
        self,
        name: str,
        image: str,
        *,
        binds: list[str],
        setup_cmds: list[str],
        registry_auth: str | None = None,
    ) -> str:
        # Reuse a live environment of the same (deterministic) name -- but only if it was
        # made for this image and these limits. Otherwise it is replaced: the work script
        # re-clones into a fresh /work, so nothing is lost but the warm-up.
        wanted = self._identity_labels(image)
        inspect = await self._request("GET", f"/containers/{name}/json", timeout=30)
        if inspect.status_code == 200:
            details = inspect.json()
            labels = (details.get("Config") or {}).get("Labels") or {}
            if all(labels.get(k) == v for k, v in wanted.items()):
                if details.get("State", {}).get("Running"):
                    return name
                await self._request("POST", f"/containers/{name}/start", timeout=60)
                return name
            await self.teardown(name)

        await self._ensure_image(image, registry_auth)
        create = await self._request(
            "POST",
            "/containers/create",
            params={"name": name},
            json_body={
                "Image": image,
                # A long-lived idle process; all real work arrives via exec.
                "Entrypoint": ["sleep"],
                "Cmd": ["infinity"],
                "HostConfig": {
                    "Binds": binds,
                    **({"NetworkMode": self._network} if self._network else {}),
                    **self._host_limits(),
                },
                "Labels": {"pyrrhula.exec_env": "1", **wanted},
            },
        )
        if create.status_code == 409:  # raced another provision of the same name
            await self._request("POST", f"/containers/{name}/start", timeout=60)
            return name
        if create.status_code >= 400:
            raise ExecEnvUnavailableError(f"create {name!r} failed: {create.text[:200]}")
        start = await self._request("POST", f"/containers/{name}/start", timeout=60)
        if start.status_code >= 400:
            raise ExecEnvUnavailableError(f"start {name!r} failed: {start.text[:200]}")

        for cmd in setup_cmds:
            result = await self.exec(name, cmd)
            if result.exit_code != 0:
                raise ExecEnvUnavailableError(
                    f"setup failed in {name!r} ({cmd[:60]!r}): {result.output[-300:]}"
                )
        return name

    @staticmethod
    def _demux(payload: bytes) -> str:
        """Docker multiplexed stream -> text (stdout+stderr, arrival order)."""
        chunks: list[bytes] = []
        i = 0
        while i + 8 <= len(payload):
            _, size = struct.unpack(">BxxxI", payload[i : i + 8])
            chunks.append(payload[i + 8 : i + 8 + size])
            i += 8 + size
        if not chunks:  # not multiplexed after all (some engines when output is empty)
            return payload.decode(errors="replace")
        return b"".join(chunks).decode(errors="replace")

    async def exec(self, env_ref: str, cmd: str, *, cwd: str | None = None) -> ExecResult:
        body: dict[str, Any] = {
            "Cmd": shell_command(cmd),
            "AttachStdout": True,
            "AttachStderr": True,
        }
        if cwd is not None:
            body["WorkingDir"] = cwd
        created = await self._request("POST", f"/containers/{env_ref}/exec", json_body=body)
        if created.status_code >= 400:
            raise ExecEnvUnavailableError(
                f"exec create in {env_ref!r} failed: {created.text[:200]}"
            )
        exec_id = created.json()["Id"]
        started = await self._request(
            "POST", f"/exec/{exec_id}/start", json_body={"Detach": False, "Tty": False}
        )
        if started.status_code >= 400:
            raise ExecEnvUnavailableError(f"exec start failed: {started.text[:200]}")
        output = self._demux(started.content)
        inspect = await self._request("GET", f"/exec/{exec_id}/json", timeout=30)
        exit_code = int(inspect.json().get("ExitCode") or 0)
        return ExecResult(exit_code=exit_code, output=output)

    async def run_script(
        self,
        name: str,
        image: str,
        script: str,
        *,
        registry_auth: str | None = None,
    ) -> ExecResult:
        """One script in a warm, name-keyed container (provisioned on first use, reused
        for the session's later reworks, torn down with the session)."""
        env_ref = await self.provision(
            name, image, binds=[], setup_cmds=[], registry_auth=registry_auth
        )
        return await self.exec(env_ref, script)

    async def teardown(self, env_ref: str) -> None:
        await self._request(
            "DELETE", f"/containers/{env_ref}", params={"force": "true"}, timeout=60
        )

    async def teardown_matching(self, prefix: str) -> int:
        listed = await self._request(
            "GET",
            "/containers/json",
            params={"all": "true", "filters": json.dumps({"name": [prefix]})},
            timeout=30,
        )
        if listed.status_code >= 400:
            return 0
        removed = 0
        for item in listed.json():
            names = [n.lstrip("/") for n in item.get("Names", [])]
            if any(n.startswith(prefix) for n in names):
                await self.teardown(item["Id"])
                removed += 1
        return removed
