"""Socket ``PreviewProvider``: a long-lived sibling container on the engine's network.

The exec-env sibling of this adapter creates ``sleep infinity`` containers and execs into
them; a preview instead runs its serving command as the container's own process and is
never exec'd. The container joins the engine-declared network (``pyrrhula-envs``), which
the api container is also on -- so ``http://{name}:8080`` resolves by container name in
both directions, and **nothing is published to a host port**: the proxy is the only way
in.

The container name is deterministic (``pyr-prev-<repo8>``), which makes both the restart
path idempotent and the DNS alias stable.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from core.ports.preview import (
    PREVIEW_PORT,
    PreviewHandle,
    PreviewUnavailableError,
)

_API = "http://d/v1.40"
_LABEL = "pyrrhula.preview"


class DockerSocketPreviewProvider:
    def __init__(self, socket_path: str, *, network: str | None = None) -> None:
        self._network = network
        self._socket = socket_path

    def _client(self, timeout: float = 120.0) -> httpx.AsyncClient:
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
        timeout: float = 120.0,
    ) -> httpx.Response:
        try:
            async with self._client(timeout) as client:
                return await client.request(
                    method, f"{_API}{path}", json=json_body, params=params, headers=headers
                )
        except httpx.HTTPError as exc:
            raise PreviewUnavailableError(f"engine socket unreachable: {exc}") from exc

    async def _ensure_image(self, image: str, registry_auth: str | None = None) -> None:
        headers = {"X-Registry-Auth": registry_auth} if registry_auth else None
        resp = await self._request(
            "POST", "/images/create", params={"fromImage": image}, headers=headers, timeout=600
        )
        if resp.status_code >= 400:
            raise PreviewUnavailableError(f"could not pull {image!r}: {resp.text[:200]}")
        for line in resp.text.splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("error"):
                raise PreviewUnavailableError(f"pull {image!r} failed: {obj['error'][:200]}")

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
        internal_url = f"http://{name}:{port}"

        # A previous preview under the same name: replace it, because the artifact it is
        # serving is the *old* build. Reuse would silently show stale content.
        inspect = await self._request("GET", f"/containers/{name}/json", timeout=30)
        if inspect.status_code == 200:
            await self.teardown(name)

        await self._ensure_image(image, registry_auth)
        create = await self._request(
            "POST",
            "/containers/create",
            params={"name": name},
            json_body={
                "Image": image,
                "Entrypoint": ["sh", "-lc"],
                "Cmd": [command],
                "Env": [f"{k}={v}" for k, v in env.items()],
                "ExposedPorts": {f"{port}/tcp": {}},
                "HostConfig": {
                    # Deliberately no PortBindings: the proxy is the only ingress.
                    **({"NetworkMode": self._network} if self._network else {}),
                },
                "Labels": {_LABEL: "1"},
            },
        )
        if create.status_code >= 400 and create.status_code != 409:
            raise PreviewUnavailableError(f"create {name!r} failed: {create.text[:200]}")
        start = await self._request("POST", f"/containers/{name}/start", timeout=60)
        if start.status_code >= 400:
            raise PreviewUnavailableError(f"start {name!r} failed: {start.text[:200]}")
        return PreviewHandle(ref=name, internal_url=internal_url)

    async def status(self, ref: str) -> str:
        inspect = await self._request("GET", f"/containers/{ref}/json", timeout=30)
        if inspect.status_code == 404:
            return "missing"
        if inspect.status_code >= 400:
            return "failed"
        state = inspect.json().get("State", {})
        if state.get("Running"):
            return "running"
        return "stopped" if int(state.get("ExitCode") or 0) == 0 else "failed"

    async def teardown(self, ref: str) -> None:
        await self._request("DELETE", f"/containers/{ref}", params={"force": "true"}, timeout=60)

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
