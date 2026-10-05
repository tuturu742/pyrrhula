"""A repo analysis that degraded to file listings refuses to be kept (it raises); the next
"Analyze repos" for the same content must then actually run, not hit the recorded
failure. Before this, fixing the assistant's model changed nothing: the idempotency row
stayed `failed` and every retry was refused at the door."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from core.actions.idempotency import OperationFailedError, idempotent
from core.tenancy.seed import seed_dev_tenant
from worker import repo_analysis


class _Repo:
    key = "demo"
    default_branch = "main"


class _Store:
    def __init__(self, _root: object) -> None:
        pass

    async def head_sha(self, _skey: str, _branch: str) -> str:
        return "abc123"

    async def read_tree(self, _skey: str, **_kw: Any) -> dict[str, str]:
        return {"README.md": "# demo"}


async def test_a_degraded_analysis_can_be_rerun(
    db_available: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"ra-{uuid.uuid4().hex[:8]}")
    attempts = 0

    @idempotent(key_fn=repo_analysis._analysis_job_key)
    async def fake_run(*, tenant_id: uuid.UUID, **kw: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("repo analysis produced no model-written summaries")
        return {"attempt": attempts}

    async def fake_get_repo(_tenant: uuid.UUID, _rid: uuid.UUID) -> _Repo:
        return _Repo()

    monkeypatch.setattr(repo_analysis, "_run_analysis", fake_run)
    monkeypatch.setattr(repo_analysis, "get_repo", fake_get_repo)
    monkeypatch.setattr(repo_analysis, "GitStore", _Store)
    monkeypatch.setattr(repo_analysis, "default_git_root", lambda: "/nonexistent")

    payload = {
        "tenant_id": str(tenant_id),
        "workspace_id": str(workspace_id),
        "repo_ids": [str(uuid.uuid4())],
    }
    with pytest.raises(RuntimeError):
        await repo_analysis.handle_analyze_workspace_repos(payload)

    # The second request for the same content runs again instead of being refused.
    try:
        result = await repo_analysis.handle_analyze_workspace_repos(payload)
    except OperationFailedError as exc:  # pragma: no cover - the regression
        raise AssertionError(f"retry was refused: {exc}") from exc
    assert result == {"attempt": 2}
    assert attempts == 2

    # A good result is then cached as before.
    assert await repo_analysis.handle_analyze_workspace_repos(payload) == {"attempt": 2}
    assert attempts == 2
