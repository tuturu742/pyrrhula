"""Kubernetes ``ExecEnvProvider``: delegated work as one-shot **Jobs**.

Spoken with plain httpx against the K8s API (no kubectl/client lib in the image).
Config comes from the engine declaration (see docs/exec-engines.md):

    {"key": "k8s", "kind": "kubernetes",
     "namespace": "pyrrhula-envs",
     "api_base": "https://...:6443",        # omit in-cluster (auto-detected)
     "token": "...", "token_file": "...",  # omit in-cluster (serviceaccount token)
     "verify_tls": false,                    # or a CA path via "ca_file"
     "image_pull_secret": "regcred",        # pre-created Secret for private images
     "job_ttl_seconds": 3600}

One-shot by nature: ``run_script`` creates a Job (unique name, labeled with the
environment name), waits for the pod to finish, and returns its logs + exit code.
``provision``/``exec`` raise ``ExecEnvUnavailableError`` -- the transport's script path
is the supported contract. Environments reach the hosted repos over git smart-HTTP
(``Settings.git_http_base`` must be routable from the cluster); no volumes are used.
``registry_auth`` (the Docker header payload) cannot be applied per-pull here -- declare
``image_pull_secret`` on the engine instead.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from core.ports.exec_env import ExecEnvUnavailableError, ExecResult

_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_LABEL = "pyrrhula.dev/exec-env"


class KubernetesExecEnvProvider:
    def __init__(
        self,
        engine: dict[str, Any],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._transport = transport
        self._namespace = str(engine.get("namespace") or "default")
        self._api_base = str(engine.get("api_base") or "").rstrip("/")
        self._token = str(engine.get("token") or "")
        self._token_file = str(engine.get("token_file") or "")
        self._verify: bool | str = engine.get("ca_file") or bool(engine.get("verify_tls", True))
        self._pull_secret = str(engine.get("image_pull_secret") or "")
        self._job_ttl = int(engine.get("job_ttl_seconds") or 3600)
        self._timeout = int(engine.get("run_timeout_seconds") or 1800)

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
            raise ExecEnvUnavailableError(
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

    async def provision(
        self,
        name: str,
        image: str,
        *,
        binds: list[str],
        setup_cmds: list[str],
        registry_auth: str | None = None,
    ) -> str:
        raise ExecEnvUnavailableError("kubernetes engine is one-shot; use run_script")

    async def exec(self, env_ref: str, cmd: str, *, cwd: str | None = None) -> ExecResult:
        raise ExecEnvUnavailableError("kubernetes engine is one-shot; use run_script")

    async def run_script(
        self,
        name: str,
        image: str,
        script: str,
        *,
        registry_auth: str | None = None,
    ) -> ExecResult:
        job_name = f"{name}-{uuid.uuid4().hex[:6]}"[:63].rstrip("-")
        spec: dict[str, Any] = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "labels": {_LABEL: name[:63]},
            },
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": self._job_ttl,
                "template": {
                    "metadata": {"labels": {_LABEL: name[:63]}},
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "work",
                                "image": image,
                                "command": ["sh", "-lc", script],
                            }
                        ],
                        **(
                            {"imagePullSecrets": [{"name": self._pull_secret}]}
                            if self._pull_secret
                            else {}
                        ),
                    },
                },
            },
        }
        try:
            return await self._run_job(job_name, spec)
        except httpx.HTTPError as exc:
            # httpx transport errors can carry an EMPTY message (observed live:
            # ConnectTimeout('')); name the failure or the job error is blank.
            raise ExecEnvUnavailableError(
                f"kubernetes api unreachable ({type(exc).__name__}): {exc or self._api_base}"
            ) from exc

    async def _run_job(self, job_name: str, spec: dict[str, Any]) -> ExecResult:
        async with self._client() as client:
            created = await client.post(
                f"/apis/batch/v1/namespaces/{self._namespace}/jobs", json=spec
            )
            if created.status_code >= 400:
                raise ExecEnvUnavailableError(
                    f"job create failed ({created.status_code}): {created.text[:200]}"
                )

            deadline = time.monotonic() + self._timeout
            pod_name = ""
            exit_code: int | None = None
            while time.monotonic() < deadline:
                pods = await client.get(
                    f"/api/v1/namespaces/{self._namespace}/pods",
                    params={"labelSelector": f"job-name={job_name}"},
                )
                items = pods.json().get("items", []) if pods.status_code == 200 else []
                if items:
                    pod = items[0]
                    pod_name = pod["metadata"]["name"]
                    statuses = (pod.get("status") or {}).get("containerStatuses") or []
                    for status in statuses:
                        terminated = (status.get("state") or {}).get("terminated")
                        if terminated is not None:
                            exit_code = int(terminated.get("exitCode", 1))
                    if exit_code is not None:
                        break
                    phase = (pod.get("status") or {}).get("phase")
                    if phase == "Failed" and not statuses:
                        exit_code = 1
                        break
                await asyncio.sleep(3)
            if exit_code is None:
                raise ExecEnvUnavailableError(f"job {job_name} did not finish in time")

            output = ""
            if pod_name:
                logs = await client.get(
                    f"/api/v1/namespaces/{self._namespace}/pods/{pod_name}/log",
                    params={"container": "work"},
                )
                if logs.status_code == 200:
                    output = logs.text
            return ExecResult(exit_code=exit_code, output=output)

    async def teardown(self, env_ref: str) -> None:
        await self.teardown_matching(env_ref)

    async def teardown_matching(self, prefix: str) -> int:
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
                    if not label.startswith(prefix):
                        continue
                    await client.delete(
                        f"/apis/batch/v1/namespaces/{self._namespace}/jobs/"
                        f"{job['metadata']['name']}",
                        params={"propagationPolicy": "Background"},
                    )
                    removed += 1
        except ExecEnvUnavailableError:
            return 0
        return removed
