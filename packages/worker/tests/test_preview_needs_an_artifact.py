"""A preview serves an artifact; starting one without an artifact must say so.

There is no button that builds a repository on its own -- the artifact is produced by a
delegation whose test command passed, then whose build command ran. Started against a
branch that never had a green build, the container used to come up, ask for the artifact,
receive a 404 and die, leaving a failed preview with nothing saying why. The sample most
likely to be previewed first is loxia, whose trunk is red on purpose, so this is the
ordinary case rather than an edge.
"""

import uuid
from typing import Any

import pytest

from worker import preview as preview_mod


class _Row:
    engine_key = None
    artifact_name = "loxia.tar.gz"
    git_ref = "master"
    session_id = None
    repo_id = None


class _EmptyStore:
    async def exists(self, key: str) -> bool:
        return False


@pytest.fixture
def _no_artifact(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    failures: list[str] = []

    async def fake_get_preview(tenant_id: uuid.UUID, preview_id: uuid.UUID) -> Any:
        return _Row()

    async def fake_mark_failed(tenant_id: uuid.UUID, preview_id: uuid.UUID, error: str) -> None:
        failures.append(error)

    def boom(*a: Any, **k: Any) -> Any:  # pragma: no cover -- must never be reached
        raise AssertionError("a provider was asked to start a container with no artifact")

    monkeypatch.setattr(preview_mod, "get_preview", fake_get_preview)
    monkeypatch.setattr(preview_mod, "mark_failed", fake_mark_failed)
    monkeypatch.setattr(preview_mod, "get_blob_store", lambda: _EmptyStore())
    monkeypatch.setattr(preview_mod, "get_preview_provider", boom)
    return failures


async def test_a_branch_with_no_build_is_refused_before_a_container_starts(
    _no_artifact: list[str],
) -> None:
    out = await preview_mod.handle_start_preview(
        {
            "tenant_id": str(uuid.uuid4()),
            "preview_id": str(uuid.uuid4()),
            "store_key": "t-loxia",
            "share_token": "tok",
        }
    )
    assert out["outcome"] == "failed"
    assert "loxia.tar.gz" in out["error"], "the operator is not told what is missing"
    assert "master" in out["error"], "the operator is not told which branch"
    # The point is that it says how to get one, not merely that there is none.
    assert "build command" in out["error"]
    assert _no_artifact and _no_artifact[0] == out["error"], "the failure was not recorded"
