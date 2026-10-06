"""AWS ECS preview adapter against a fake ECS client. No AWS.

Previews are found by ``startedBy`` (the name's first 36 characters). A repo's no-branch
preview ``pyr-prev-<repo8>`` is a prefix of its branch previews' names, so stopping or
replacing one preview must match that name exactly, never by prefix.
"""

from __future__ import annotations

from typing import Any

from adapters.preview.aws_ecs import AwsEcsPreviewProvider

_PLAIN = "pyr-prev-6a23d529"
_BRANCH = "pyr-prev-6a23d529-pyr-45dd0469-3"


class _FakeEcs:
    def __init__(self) -> None:
        self.tasks = {
            "arn:plain": {
                "taskArn": "arn:plain",
                "startedBy": _PLAIN[:36],
                "lastStatus": "RUNNING",
            },
            "arn:branch": {
                "taskArn": "arn:branch",
                "startedBy": _BRANCH[:36],
                "lastStatus": "PENDING",
            },
        }
        self.stopped: list[str] = []

    def list_tasks(self, **_: Any) -> dict[str, Any]:
        return {"taskArns": list(self.tasks)}

    def describe_tasks(self, *, cluster: str, tasks: list[str]) -> dict[str, Any]:
        return {"tasks": [self.tasks[a] for a in tasks]}

    def stop_task(self, *, cluster: str, task: str, reason: str) -> None:
        self.stopped.append(task)


def _provider(ecs: _FakeEcs) -> AwsEcsPreviewProvider:
    return AwsEcsPreviewProvider(
        {"cluster": "c", "region": "r", "subnets": ["s"]}, client_factory=lambda _: ecs
    )


async def test_teardown_stops_only_that_preview_not_the_branches_it_prefixes() -> None:
    ecs = _FakeEcs()
    await _provider(ecs).teardown(_PLAIN)
    assert ecs.stopped == ["arn:plain"]


async def test_status_reads_its_own_task_not_a_branch_preview() -> None:
    ecs = _FakeEcs()
    del ecs.tasks["arn:plain"]
    # Only the branch preview is running; the no-branch one is gone.
    assert await _provider(ecs).status(_PLAIN) == "missing"


async def test_teardown_matching_still_sweeps_by_prefix() -> None:
    ecs = _FakeEcs()
    assert await _provider(ecs).teardown_matching(_PLAIN) == 2
    assert sorted(ecs.stopped) == ["arn:branch", "arn:plain"]
