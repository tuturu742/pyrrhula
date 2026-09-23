"""The G4.17 review/merge glue in worker/review.py, without a DB or network: the DB and
remote seams are monkeypatched so the test asserts the *decisions* -- file a formal
review, fall back to a comment when the host refuses, and merge under the acting token.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import worker.review as review


class FakeRemote:
    def __init__(self, *, review_ok: bool = True, merge_ok: bool = True) -> None:
        self.review_ok, self.merge_ok = review_ok, merge_ok
        self.calls: list[tuple[str, object]] = []

    async def submit_review(self, url, token, *, number, verdict, body):  # noqa: ANN001
        self.calls.append(("review", {"token": token, "verdict": verdict, "number": number}))
        return self.review_ok

    async def post_pr_comment(self, url, token, *, number, body):  # noqa: ANN001
        self.calls.append(("comment", {"token": token, "number": number}))
        return True

    async def merge_pull_request(self, url, token, *, number, method="merge"):  # noqa: ANN001
        self.calls.append(("merge", {"token": token, "number": number}))
        return self.merge_ok


def _patch(monkeypatch, remote: FakeRemote, cred_ref: uuid.UUID | None) -> None:
    async def fake_get_repo(_t, _r):
        return SimpleNamespace(source_url="https://github.com/o/r", provider="github")

    async def fake_resolve_identity(_t, _r, _p):
        return cred_ref

    async def fake_key(_t, _ref, *, encryptor):  # noqa: ANN001
        return "TOK_" + str(_ref)[:4]

    monkeypatch.setattr(review, "get_repo", fake_get_repo)
    monkeypatch.setattr(review, "resolve_git_identity", fake_resolve_identity)
    monkeypatch.setattr(review, "resolve_connection_api_key", fake_key)
    monkeypatch.setattr(review, "resolve_remote", lambda url, provider: remote)
    monkeypatch.setattr(review, "get_encryptor", lambda: object())


_PR = {"html_url": "https://github.com/o/r/pull/5", "pr_ref": "#5"}


@pytest.mark.asyncio
async def test_formal_review_filed_under_reviewer_token(monkeypatch) -> None:  # noqa: ANN001
    remote = FakeRemote(review_ok=True)
    _patch(monkeypatch, remote, uuid.uuid4())
    await review._post_remote_review(
        uuid.uuid4(), str(uuid.uuid4()), uuid.uuid4(), _PR, approve=True, body="LGTM"
    )
    assert [c[0] for c in remote.calls] == ["review"]
    assert remote.calls[0][1]["verdict"] == "approve"
    assert remote.calls[0][1]["token"].startswith("TOK_")


@pytest.mark.asyncio
async def test_review_falls_back_to_comment_when_formal_refused(monkeypatch) -> None:  # noqa: ANN001
    remote = FakeRemote(review_ok=False)  # e.g. GitHub 422: reviewer == PR author
    _patch(monkeypatch, remote, uuid.uuid4())
    await review._post_remote_review(
        uuid.uuid4(), str(uuid.uuid4()), uuid.uuid4(), _PR, approve=False, body="fix it"
    )
    assert [c[0] for c in remote.calls] == ["review", "comment"]


@pytest.mark.asyncio
async def test_no_credential_means_no_remote_call(monkeypatch) -> None:  # noqa: ANN001
    remote = FakeRemote()
    _patch(monkeypatch, remote, None)  # neither persona nor repo has a credential
    await review._post_remote_review(
        uuid.uuid4(), str(uuid.uuid4()), uuid.uuid4(), _PR, approve=True, body="x"
    )
    assert remote.calls == []


@pytest.mark.asyncio
async def test_merge_remote_calls_merge_under_token(monkeypatch) -> None:  # noqa: ANN001
    remote = FakeRemote(merge_ok=True)
    _patch(monkeypatch, remote, uuid.uuid4())
    ok = await review._merge_remote(uuid.uuid4(), str(uuid.uuid4()), uuid.uuid4(), _PR)
    assert ok is True
    assert [c[0] for c in remote.calls] == ["merge"]


@pytest.mark.asyncio
async def test_merge_remote_false_when_gate_refuses(monkeypatch) -> None:  # noqa: ANN001
    remote = FakeRemote(merge_ok=False)  # host gate: required review not landed
    _patch(monkeypatch, remote, uuid.uuid4())
    ok = await review._merge_remote(uuid.uuid4(), str(uuid.uuid4()), uuid.uuid4(), _PR)
    assert ok is False


# ── which branch the work was branched from ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_base_branch_follows_the_repository_not_a_hardcoded_main(monkeypatch) -> None:  # noqa: ANN001
    """`GitStore`'s diff helpers default to `main`, a fine default and a wrong answer for
    a repository whose trunk is called something else. The review then cannot read the
    diff, the job fails, and the work item sits in `in_review` with no verdict -- so a
    delegation loop delivered pull requests that nobody could approve."""

    async def fake_get_repo(_t, _r):
        return SimpleNamespace(source_url="https://github.com/o/r", default_branch="master")

    monkeypatch.setattr(review, "get_repo", fake_get_repo)
    assert await review._base_branch(uuid.uuid4(), str(uuid.uuid4())) == "master"


@pytest.mark.asyncio
async def test_an_explicit_base_wins_over_the_repository_default(monkeypatch) -> None:  # noqa: ANN001
    """A delegation may be told to branch from a release line rather than the trunk. The
    review has to diff against whatever the work was actually branched from, which is why
    the delegation carries it into the review job rather than letting it be re-derived."""

    async def fake_get_repo(_t, _r):  # pragma: no cover -- must not be consulted
        raise AssertionError("an explicit base must not need the repo row")

    monkeypatch.setattr(review, "get_repo", fake_get_repo)
    resolved = await review._base_branch(uuid.uuid4(), str(uuid.uuid4()), "release/2.1")
    assert resolved == "release/2.1"


@pytest.mark.asyncio
async def test_no_repository_still_answers_main(monkeypatch) -> None:  # noqa: ANN001
    """A store-only delegation has no repo row; `main` is what GitStore initialises."""
    assert await review._base_branch(uuid.uuid4(), None) == "main"
