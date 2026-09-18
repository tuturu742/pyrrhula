"""AwsEcsExecEnvProvider against fake boto3 clients (no AWS, no network)."""

from __future__ import annotations

from typing import Any

import pytest

from adapters.exec_env.aws_ecs import AwsEcsExecEnvProvider
from core.ports.exec_env import ExecEnvUnavailableError

_ENGINE = {
    "key": "aws",
    "kind": "aws-ecs",
    "region": "eu-central-1",
    "cluster": "pyrrhula",
    "subnets": ["subnet-1"],
    "security_groups": ["sg-1"],
    "execution_role_arn": "arn:aws:iam::1:role/exec",
    "log_group": "/pyrrhula/exec-envs",
    "run_timeout_seconds": 30,
}


class FakeEcs:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.registered: list[dict[str, Any]] = []
        self.run_calls: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        self.families: dict[str, str] = {}
        self.running_started_by: list[str] = []

    def describe_task_definition(self, taskDefinition: str) -> dict[str, Any]:  # noqa: N803
        if taskDefinition not in self.families:
            raise RuntimeError("ClientException: Unable to describe task definition")
        return {
            "taskDefinition": {
                "taskDefinitionArn": f"arn:td/{taskDefinition}:1",
                "containerDefinitions": [{"image": self.families[taskDefinition]}],
            }
        }

    def register_task_definition(self, **params: Any) -> dict[str, Any]:
        self.registered.append(params)
        self.families[params["family"]] = params["containerDefinitions"][0]["image"]
        return {"taskDefinition": {"taskDefinitionArn": f"arn:td/{params['family']}:1"}}

    def run_task(self, **params: Any) -> dict[str, Any]:
        self.run_calls.append(params)
        return {"tasks": [{"taskArn": "arn:aws:ecs:task/cluster/abc123"}], "failures": []}

    def describe_tasks(self, cluster: str, tasks: list[str]) -> dict[str, Any]:
        return {
            "tasks": [
                {
                    "taskArn": arn,
                    "lastStatus": "STOPPED",
                    "startedBy": (self.running_started_by or ["pyr-env-x"])[0],
                    "containers": [{"name": "work", "exitCode": self.exit_code}],
                }
                for arn in tasks
            ]
        }

    def list_tasks(self, **params: Any) -> dict[str, Any]:
        return {"taskArns": ["arn:aws:ecs:task/cluster/abc123"] if self.running_started_by else []}

    def stop_task(self, cluster: str, task: str, reason: str) -> dict[str, Any]:
        self.stopped.append(task)
        return {}


class FakeLogs:
    def get_log_events(self, **params: Any) -> dict[str, Any]:
        return {
            "events": [{"message": "PYR_STEP=clone"}, {"message": "PYR_TEST_RC=0"}],
            "nextForwardToken": params.get("nextToken") or "t1",
        }


def _provider(ecs: FakeEcs, engine: dict[str, Any] = _ENGINE) -> AwsEcsExecEnvProvider:
    logs = FakeLogs()
    return AwsEcsExecEnvProvider(engine, client_factory=lambda svc: ecs if svc == "ecs" else logs)


async def test_run_script_registers_and_runs() -> None:
    ecs = FakeEcs()
    result = await _provider(ecs).run_script("pyr-env-abcd1234-r1", "node:22-bookworm", "echo hi")
    assert result.exit_code == 0
    assert "PYR_TEST_RC=0" in result.output
    assert len(ecs.registered) == 1
    td = ecs.registered[0]
    assert td["requiresCompatibilities"] == ["FARGATE"]
    assert td["containerDefinitions"][0]["image"] == "node:22-bookworm"
    assert (
        td["containerDefinitions"][0]["logConfiguration"]["options"]["awslogs-group"]
        == "/pyrrhula/exec-envs"
    )
    run = ecs.run_calls[0]
    assert run["startedBy"] == "pyr-env-abcd1234-r1"
    command = run["overrides"]["containerOverrides"][0]["command"]
    assert command[:2] == ["sh", "-c"]
    # Not a login shell: /etc/profile assigns PATH and would discard the image's own,
    # which is where official toolchain images put their toolchain. See exec_env.shell.
    assert command[2].endswith("echo hi")
    assert "/etc/profile" in command[2]
    assert run["networkConfiguration"]["awsvpcConfiguration"]["subnets"] == ["subnet-1"]


async def test_run_script_reuses_task_definition_per_image() -> None:
    ecs = FakeEcs()
    provider = _provider(ecs)
    await provider.run_script("pyr-env-a-1", "node:22-bookworm", "true")
    await provider.run_script("pyr-env-a-2", "node:22-bookworm", "true")
    assert len(ecs.registered) == 1  # second run found the family
    await provider.run_script("pyr-env-a-3", "python:3.12", "true")
    assert len(ecs.registered) == 2


async def test_nonzero_exit_code_propagates() -> None:
    ecs = FakeEcs(exit_code=90)
    result = await _provider(ecs).run_script("pyr-env-b-1", "img", "exit 90")
    assert result.exit_code == 90


async def test_one_shot_contract() -> None:
    provider = _provider(FakeEcs())
    with pytest.raises(ExecEnvUnavailableError):
        await provider.provision("n", "img", binds=[], setup_cmds=[])
    with pytest.raises(ExecEnvUnavailableError):
        await provider.exec("n", "true")


async def test_teardown_matching_stops_by_started_by_prefix() -> None:
    ecs = FakeEcs()
    ecs.running_started_by = ["pyr-env-abcd1234-r1"]
    assert await _provider(ecs).teardown_matching("pyr-env-abcd1234-") == 1
    assert ecs.stopped
    assert await _provider(ecs).teardown_matching("pyr-env-zzzz") == 0


def test_the_images_path_survives_profile_sourcing() -> None:
    """Regression: every adapter ran `sh -lc`, so /etc/profile's PATH assignment discarded
    the image's. `rust:*-bookworm` exports its toolchain at /usr/local/cargo/bin and nowhere
    else, so `rustup component add rustfmt clippy` failed with `rustup: not found` against a
    perfectly good image. The image's PATH must come back in front after profile runs."""
    from adapters.exec_env.shell import shell_command

    argv = shell_command("cargo test")
    assert argv[0] == "sh"
    assert argv[1] == "-c", "a login shell re-assigns PATH and loses the image's"
    script = argv[2]
    assert script.index('_pyr_path="$PATH"') < script.index("/etc/profile"), (
        "the image's PATH must be captured before /etc/profile can overwrite it"
    )
    assert script.index("/etc/profile") < script.index('PATH="$_pyr_path'), (
        "the image's PATH must be restored after profile sourcing, not before"
    )
    assert script.endswith("cargo test")
