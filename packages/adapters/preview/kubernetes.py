"""Kubernetes ``PreviewProvider``: a long-lived preview as a **Job that never exits**.

A Job whose container serves HTTP forever is a long-running pod, and that matters for
one specific reason: the runner ServiceAccount is granted ``batch/jobs:
create,get,list,delete`` and ``pods: get,list`` and nothing else (deploy/k8s/engine-rbac.yaml).
Modelling a preview as a Deployment + Service would need new RBAC in two files; a Job
needs none, and we read the address straight off ``pod.status.podIP``.

It is also the better fit: ``activeDeadlineSeconds`` makes the cluster enforce the
preview's TTL even if the worker is down, and ``ttlSecondsAfterFinished`` garbage-collects
the object afterwards. The trade-off is no restart-on-crash -- which is correct here. A
crashed preview should surface as ``failed``, not quietly loop.

Reaching the pod needs no NetworkPolicy change: envs-networkpolicy.yaml declares
``policyTypes: ["Egress"]`` only, so ingress to these pods is unrestricted.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from core.ports.preview import PREVIEW_PORT, PreviewHandle, PreviewUnavailableError

_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_LABEL = "pyrrhula.dev/preview"


class KubernetesPreviewProvider:
    def __init__(
        self, engine: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._transport = transport
        self._namespace = str(engine.get("namespace") or "default")
        self._api_base = str(engine.get("api_base") or "").rstrip("/")
        self._token = str(engine.get("token") or "")
        self._token_file = str(engine.get("token_file") or "")
        self._verify: bool | str = engine.get("ca_file") or bool(engine.get("verify_tls", True))
        self._pull_secret = str(engine.get("image_pull_secret") or "")
        # How long to wait for the pod to get an IP -- not how long the preview lives.
        self._start_timeout = int(engine.get("preview_start_timeout_seconds") or 180)

    def _resolve_auth(self) -> tuple[str, str, bool | str]:
        api_base, token, verify = self._api_base, self._token, self._verify
        if not token:
            token_file = Path(self._token_file) if self._token_file else _SA_DIR / "token"
            if token_file.exists():
                token = token_file.read_text().strip()
        if not api_base and (_SA_DIR / "token").exists():
            api_base = "https://kubernetes.default.svc"
            ca = _SA_DIR / "ca.crt"
            if verify is True and ca.exists():
                verify = str(ca)
        if not api_base or not token:
            raise PreviewUnavailableError(
                "kubernetes engine: no api_base/token (declare them or run in-cluster)"
            )
        return api_base, token, verify

    def _client(self) -> httpx.AsyncClient:
        api_base, token, verify = self._resolve_auth()
        return httpx.AsyncClient(
            base_url=api_base,
            headers={"Authorization": f"Bearer {token}"},
            verify=verify,
            timeout=60,
            transport=self._transport,
        )

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
        # Replace any previous preview under this name -- it serves the old artifact. Only
        # this name: a repo's no-branch preview is a prefix of its branch previews' names.
        await self.teardown(name)

        job_name = f"{name}-{uuid.uuid4().hex[:6]}"[:63].rstrip("-")
        pod_spec: dict[str, Any] = {
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "preview",
                    "image": image,
                    "command": ["sh", "-lc", command],
                    "env": [{"name": k, "value": v} for k, v in env.items()],
                    "ports": [{"containerPort": port}],
                }
            ],
            **({"imagePullSecrets": [{"name": self._pull_secret}]} if self._pull_secret else {}),
        }
        job_spec: dict[str, Any] = {
            "backoffLimit": 0,
            "template": {"metadata": {"labels": {_LABEL: name[:63]}}, "spec": pod_spec},
        }
        if ttl_seconds:
            # The cluster expires the preview even if the worker never runs again.
            job_spec["activeDeadlineSeconds"] = int(ttl_seconds)
            job_spec["ttlSecondsAfterFinished"] = 300
        spec = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": job_name, "labels": {_LABEL: name[:63]}},
            "spec": job_spec,
        }

        try:
            async with self._client() as client:
                created = await client.post(
                    f"/apis/batch/v1/namespaces/{self._namespace}/jobs", json=spec
                )
                if created.status_code >= 400:
                    raise PreviewUnavailableError(
                        f"preview job create failed ({created.status_code}): {created.text[:200]}"
                    )
                pod_ip = await self._await_pod_ip(client, job_name)
        except httpx.HTTPError as exc:
            raise PreviewUnavailableError(
                f"kubernetes api unreachable ({type(exc).__name__}): {exc or self._api_base}"
            ) from exc
        return PreviewHandle(ref=name, internal_url=f"http://{pod_ip}:{port}")

    async def _await_pod_ip(self, client: httpx.AsyncClient, job_name: str) -> str:
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            pods = await client.get(
                f"/api/v1/namespaces/{self._namespace}/pods",
                params={"labelSelector": f"job-name={job_name}"},
            )
            for pod in pods.json().get("items", []) if pods.status_code == 200 else []:
                status = pod.get("status") or {}
                phase = status.get("phase")
                if phase == "Failed":
                    raise PreviewUnavailableError(
                        f"preview pod failed to start: {status.get('reason') or phase}"
                    )
                # Running with an IP is the signal; the server binds immediately after
                # its (short) download+extract, and the proxy retries a cold connection.
                if phase == "Running" and status.get("podIP"):
                    return str(status["podIP"])
            await asyncio.sleep(2)
        raise PreviewUnavailableError(f"preview pod for {job_name} never became ready")

    async def status(self, ref: str) -> str:
        try:
            async with self._client() as client:
                pods = await client.get(
                    f"/api/v1/namespaces/{self._namespace}/pods",
                    params={"labelSelector": f"{_LABEL}={ref[:63]}"},
                )
                items = pods.json().get("items", []) if pods.status_code == 200 else []
                if not items:
                    return "missing"
                phase = ((items[0].get("status") or {}).get("phase")) or ""
                if phase == "Running":
                    return "running"
                if phase == "Failed":
                    return "failed"
                return "stopped" if phase == "Succeeded" else "starting"
        except (httpx.HTTPError, PreviewUnavailableError):
            return "missing"

    async def teardown(self, ref: str) -> None:
        """Stop exactly this preview. Not a prefix match: ``pyr-prev-<repo8>`` (no branch)
        is a prefix of ``pyr-prev-<repo8>-<branch>``, and stopping one must not stop the
        others."""
        await self._delete_jobs(lambda label: label == ref[:63])

    async def teardown_matching(self, prefix: str) -> int:
        return await self._delete_jobs(lambda label: label.startswith(prefix))

    async def _delete_jobs(self, matches: Callable[[str], bool]) -> int:
        removed = 0
        try:
            async with self._client() as client:
                jobs = await client.get(
                    f"/apis/batch/v1/namespaces/{self._namespace}/jobs",
                    params={"labelSelector": _LABEL},
                )
                if jobs.status_code != 200:
                    return 0
                for job in jobs.json().get("items", []):
                    label = (job["metadata"].get("labels") or {}).get(_LABEL, "")
                    if not matches(label):
                        continue
                    await client.delete(
                        f"/apis/batch/v1/namespaces/{self._namespace}/jobs/"
                        f"{job['metadata']['name']}",
                        params={"propagationPolicy": "Background"},
                    )
                    removed += 1
        except (httpx.HTTPError, PreviewUnavailableError):
            return 0
        return removed
