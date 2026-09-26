"""AWS ECS (Fargate) ``PreviewProvider``: a long-lived preview as a **RunTask**.

**EXPERIMENTAL -- unverified.** See ``adapters/exec_env/aws_ecs.py``; the same caveat.

Deliberately a task, not a service. The worker's IAM policy grants
``RunTask``/``DescribeTasks``/``ListTasks``/``StopTask`` and does **not** grant
``CreateService`` or ``servicediscovery:*`` -- so a task needs no new IAM, while a service
would need both plus Cloud Map to get a name. We don't need a name: the proxy talks to the
task's private IP, read off the ENI attachment.

The api task reaches it over a security-group rule from the app to the env subnets.

ECS cannot override an image at RunTask time, so this reuses the exec-env adapter's
convention of a task-definition family per image.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

from core.ports.preview import PREVIEW_PORT, PreviewHandle, PreviewUnavailableError

_FAMILY_PREFIX = "pyrrhula-preview-"
_CONTAINER = "preview"
_STREAM_PREFIX = "pyr"


class AwsEcsPreviewProvider:
    def __init__(self, engine: dict[str, Any], client_factory: Any | None = None) -> None:
        self._region = str(engine.get("region") or "")
        self._cluster = str(engine.get("cluster") or "")
        self._subnets = [str(s) for s in engine.get("subnets") or []]
        self._security_groups = [str(s) for s in engine.get("security_groups") or []]
        self._execution_role = str(engine.get("execution_role_arn") or "")
        self._task_role = str(engine.get("task_role_arn") or "")
        self._log_group = str(engine.get("log_group") or "")
        self._cpu = str(engine.get("preview_cpu") or engine.get("cpu") or "512")
        self._memory = str(engine.get("preview_memory") or engine.get("memory") or "1024")
        self._public_ip = "ENABLED" if engine.get("assign_public_ip", True) else "DISABLED"
        self._start_timeout = int(engine.get("preview_start_timeout_seconds") or 300)
        self._client_factory = client_factory

    def _client(self, service: str) -> Any:
        if self._client_factory is not None:
            return self._client_factory(service)
        try:
            import boto3
        except ImportError as exc:
            raise PreviewUnavailableError(
                "aws-ecs engine declared but boto3 is not installed"
            ) from exc
        if not (self._region and self._cluster and self._subnets):
            raise PreviewUnavailableError(
                "aws-ecs engine: region, cluster and subnets are required"
            )
        return boto3.client(service, region_name=self._region)

    def _ensure_task_definition(self, ecs: Any, image: str, port: int) -> str:
        family = _FAMILY_PREFIX + hashlib.sha256(image.encode()).hexdigest()[:12]
        try:
            described = ecs.describe_task_definition(taskDefinition=family)
            current = described["taskDefinition"]
            if current["containerDefinitions"][0]["image"] == image:
                return str(current["taskDefinitionArn"])
        except Exception:  # noqa: BLE001 -- family does not exist yet
            pass
        container: dict[str, Any] = {
            "name": _CONTAINER,
            "image": image,
            "essential": True,
            "portMappings": [{"containerPort": port, "protocol": "tcp"}],
        }
        if self._log_group:
            container["logConfiguration"] = {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": self._log_group,
                    "awslogs-region": self._region,
                    "awslogs-stream-prefix": _STREAM_PREFIX,
                },
            }
        params: dict[str, Any] = {
            "family": family,
            "requiresCompatibilities": ["FARGATE"],
            "networkMode": "awsvpc",
            "cpu": self._cpu,
            "memory": self._memory,
            "containerDefinitions": [container],
        }
        if self._execution_role:
            params["executionRoleArn"] = self._execution_role
        if self._task_role:
            params["taskRoleArn"] = self._task_role
        registered = ecs.register_task_definition(**params)
        return str(registered["taskDefinition"]["taskDefinitionArn"])

    @staticmethod
    def _private_ip(task: dict[str, Any]) -> str:
        for attachment in task.get("attachments") or []:
            for detail in attachment.get("details") or []:
                if detail.get("name") == "privateIPv4Address" and detail.get("value"):
                    return str(detail["value"])
        return ""

    def _start(
        self, name: str, image: str, command: str, env: dict[str, str], port: int
    ) -> PreviewHandle:
        ecs = self._client("ecs")
        self._stop_matching(ecs, name)  # the old task serves the old artifact
        task_def = self._ensure_task_definition(ecs, image, port)
        started = ecs.run_task(
            cluster=self._cluster,
            taskDefinition=task_def,
            launchType="FARGATE",
            startedBy=name[:36],
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": self._subnets,
                    "securityGroups": self._security_groups,
                    "assignPublicIp": self._public_ip,
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": _CONTAINER,
                        "command": ["sh", "-lc", command],
                        "environment": [{"name": k, "value": v} for k, v in env.items()],
                    }
                ]
            },
        )
        failures = started.get("failures") or []
        if failures or not started.get("tasks"):
            reason = failures[0].get("reason", "unknown") if failures else "no task returned"
            raise PreviewUnavailableError(f"ecs run_task failed: {reason}")
        task_arn = started["tasks"][0]["taskArn"]

        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            described = ecs.describe_tasks(cluster=self._cluster, tasks=[task_arn])
            tasks = described.get("tasks") or []
            if tasks:
                task = tasks[0]
                last = task.get("lastStatus")
                if last == "STOPPED":
                    raise PreviewUnavailableError(
                        f"preview task stopped on startup: {task.get('stoppedReason') or ''}"
                    )
                ip = self._private_ip(task)
                if last == "RUNNING" and ip:
                    return PreviewHandle(ref=name, internal_url=f"http://{ip}:{port}")
            time.sleep(5)
        ecs.stop_task(cluster=self._cluster, task=task_arn, reason="pyrrhula preview timeout")
        raise PreviewUnavailableError(f"preview task for {name} never became ready")

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
        # ECS has no native task deadline -- the worker's reaper enforces ttl_seconds.
        return await asyncio.to_thread(self._start, name, image, command, env, port)

    def _describe_by_started_by(self, ecs: Any, prefix: str) -> list[dict[str, Any]]:
        arns: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"cluster": self._cluster, "desiredStatus": "RUNNING"}
            if token:
                kwargs["nextToken"] = token
            page = ecs.list_tasks(**kwargs)
            arns.extend(page.get("taskArns") or [])
            token = page.get("nextToken")
            if not token:
                break
        matched: list[dict[str, Any]] = []
        for start in range(0, len(arns), 100):
            described = ecs.describe_tasks(cluster=self._cluster, tasks=arns[start : start + 100])
            for task in described.get("tasks") or []:
                if str(task.get("startedBy") or "").startswith(prefix):
                    matched.append(task)
        return matched

    async def status(self, ref: str) -> str:
        def _status() -> str:
            try:
                ecs = self._client("ecs")
                tasks = self._describe_by_started_by(ecs, ref)
            except PreviewUnavailableError:
                return "missing"
            if not tasks:
                return "missing"
            last = tasks[0].get("lastStatus")
            if last == "RUNNING":
                return "running"
            return "stopped" if last == "STOPPED" else "starting"

        return await asyncio.to_thread(_status)

    def _stop_matching(self, ecs: Any, prefix: str) -> int:
        removed = 0
        for task in self._describe_by_started_by(ecs, prefix):
            ecs.stop_task(
                cluster=self._cluster, task=task["taskArn"], reason="pyrrhula preview teardown"
            )
            removed += 1
        return removed

    async def teardown(self, ref: str) -> None:
        await self.teardown_matching(ref)

    async def teardown_matching(self, prefix: str) -> int:
        def _teardown() -> int:
            try:
                return self._stop_matching(self._client("ecs"), prefix)
            except PreviewUnavailableError:
                return 0

        return await asyncio.to_thread(_teardown)
