"""AWS ECS (Fargate) ``ExecEnvProvider``: delegated work as one-shot **tasks**.

Engine declaration (see docs/exec-engines.md):

    {"key": "aws", "kind": "aws-ecs",
     "region": "eu-central-1",
     "cluster": "pyrrhula",
     "subnets": ["subnet-..."], "security_groups": ["sg-..."],
     "execution_role_arn": "arn:aws:iam::...:role/pyrrhula-exec",  # ECR pull + awslogs
     "log_group": "/pyrrhula/exec-envs",
     "task_role_arn": "",                 # optional, for the workload itself
     "cpu": "1024", "memory": "2048",
     "assign_public_ip": true,             # false when the subnets have a NAT route
     "run_timeout_seconds": 1800}

ECS cannot override a task's image at RunTask time, so ``run_script`` registers (once,
idempotently) a task-definition revision per image -- family ``pyrrhula-env-<sha12>`` --
and runs that with the script as the container command. The environment name rides
``startedBy`` (<=36 chars), which is what ``teardown_matching`` filters on. Logs come
from CloudWatch (awslogs driver, stream ``pyr/work/<task-id>``); exit code from the
stopped task's container. ``registry_auth`` (the Docker header payload) cannot be
applied per-run here -- use ECR, or bake ``repositoryCredentials`` into a pre-registered
task definition.

boto3 is imported lazily: deployments that never declare an aws-ecs engine don't need
it installed. Credentials use the default AWS chain (task role / env vars / profile).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

from adapters.exec_env.shell import shell_command
from core.ports.exec_env import ExecEnvUnavailableError, ExecResult

_FAMILY_PREFIX = "pyrrhula-env-"
_CONTAINER = "work"
_STREAM_PREFIX = "pyr"


class AwsEcsExecEnvProvider:
    def __init__(self, engine: dict[str, Any], client_factory: Any | None = None) -> None:
        self._region = str(engine.get("region") or "")
        self._cluster = str(engine.get("cluster") or "")
        self._subnets = [str(s) for s in engine.get("subnets") or []]
        self._security_groups = [str(s) for s in engine.get("security_groups") or []]
        self._execution_role = str(engine.get("execution_role_arn") or "")
        self._task_role = str(engine.get("task_role_arn") or "")
        self._log_group = str(engine.get("log_group") or "")
        self._cpu = str(engine.get("cpu") or "1024")
        self._memory = str(engine.get("memory") or "2048")
        self._public_ip = "ENABLED" if engine.get("assign_public_ip", True) else "DISABLED"
        self._timeout = int(engine.get("run_timeout_seconds") or 1800)
        self._client_factory = client_factory

    def _client(self, service: str) -> Any:
        if self._client_factory is not None:
            return self._client_factory(service)
        try:
            import boto3
        except ImportError as exc:
            raise ExecEnvUnavailableError(
                "aws-ecs engine declared but boto3 is not installed"
            ) from exc
        if not (self._region and self._cluster and self._subnets):
            raise ExecEnvUnavailableError(
                "aws-ecs engine: region, cluster and subnets are required"
            )
        return boto3.client(service, region_name=self._region)

    async def provision(
        self,
        name: str,
        image: str,
        *,
        binds: list[str],
        setup_cmds: list[str],
        registry_auth: str | None = None,
    ) -> str:
        raise ExecEnvUnavailableError("aws-ecs engine is one-shot; use run_script")

    async def exec(self, env_ref: str, cmd: str, *, cwd: str | None = None) -> ExecResult:
        raise ExecEnvUnavailableError("aws-ecs engine is one-shot; use run_script")

    def _ensure_task_definition(self, ecs: Any, image: str) -> str:
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

    def _run(self, name: str, image: str, script: str) -> ExecResult:
        ecs = self._client("ecs")
        task_def = self._ensure_task_definition(ecs, image)
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
                "containerOverrides": [{"name": _CONTAINER, "command": shell_command(script)}]
            },
        )
        failures = started.get("failures") or []
        if failures or not started.get("tasks"):
            reason = failures[0].get("reason", "unknown") if failures else "no task returned"
            raise ExecEnvUnavailableError(f"ecs run_task failed: {reason}")
        task_arn = started["tasks"][0]["taskArn"]
        task_id = task_arn.rsplit("/", 1)[-1]

        deadline = time.monotonic() + self._timeout
        task: dict[str, Any] = {}
        while time.monotonic() < deadline:
            described = ecs.describe_tasks(cluster=self._cluster, tasks=[task_arn])
            tasks = described.get("tasks") or []
            if tasks:
                task = tasks[0]
                if task.get("lastStatus") == "STOPPED":
                    break
            time.sleep(5)
        else:
            ecs.stop_task(cluster=self._cluster, task=task_arn, reason="pyrrhula timeout")
            raise ExecEnvUnavailableError(f"ecs task {task_id} did not finish in time")

        containers = task.get("containers") or []
        exit_code = 1
        for container in containers:
            if container.get("name") == _CONTAINER and "exitCode" in container:
                exit_code = int(container["exitCode"])
        output = self._read_logs(task_id)
        if not output and exit_code != 0:
            output = str(task.get("stoppedReason") or "")
        return ExecResult(exit_code=exit_code, output=output)

    def _read_logs(self, task_id: str) -> str:
        if not self._log_group:
            return ""
        logs = self._client("logs")
        stream = f"{_STREAM_PREFIX}/{_CONTAINER}/{task_id}"
        lines: list[str] = []
        token: str | None = None
        try:
            while True:
                kwargs: dict[str, Any] = {
                    "logGroupName": self._log_group,
                    "logStreamName": stream,
                    "startFromHead": True,
                }
                if token:
                    kwargs["nextToken"] = token
                page = logs.get_log_events(**kwargs)
                lines.extend(e.get("message", "") for e in page.get("events", []))
                next_token = page.get("nextForwardToken")
                if not next_token or next_token == token:
                    break
                token = next_token
        except Exception:  # noqa: BLE001 -- logs are best-effort; the exit code is the signal
            pass
        return "\n".join(lines)

    async def run_script(
        self,
        name: str,
        image: str,
        script: str,
        *,
        registry_auth: str | None = None,
    ) -> ExecResult:
        return await asyncio.to_thread(self._run, name, image, script)

    async def teardown(self, env_ref: str) -> None:
        await self.teardown_matching(env_ref)

    async def teardown_matching(self, prefix: str) -> int:
        return await asyncio.to_thread(self._teardown_matching, prefix)

    def _teardown_matching(self, prefix: str) -> int:
        removed = 0
        try:
            ecs = self._client("ecs")
            arns: list[str] = []
            token: str | None = None
            while True:
                kwargs: dict[str, Any] = {
                    "cluster": self._cluster,
                    "desiredStatus": "RUNNING",
                }
                if token:
                    kwargs["nextToken"] = token
                page = ecs.list_tasks(**kwargs)
                arns.extend(page.get("taskArns") or [])
                token = page.get("nextToken")
                if not token:
                    break
            for start in range(0, len(arns), 100):
                described = ecs.describe_tasks(
                    cluster=self._cluster, tasks=arns[start : start + 100]
                )
                for task in described.get("tasks") or []:
                    if str(task.get("startedBy") or "").startswith(prefix):
                        ecs.stop_task(
                            cluster=self._cluster,
                            task=task["taskArn"],
                            reason="pyrrhula teardown",
                        )
                        removed += 1
        except ExecEnvUnavailableError:
            return 0
        return removed
