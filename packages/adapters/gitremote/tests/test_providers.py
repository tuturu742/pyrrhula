"""Provider adapters against ``httpx.MockTransport``: URL parsing (incl. self-hosted
and GitLab subgroups), find-vs-create PR flows, comments, and push userinfo
conventions. No network."""

from __future__ import annotations

import json

import httpx
import pytest

from adapters.gitremote.base import GenericRemote, parse_remote_url
from adapters.gitremote.gitea import GiteaRemote
from adapters.gitremote.github import GithubRemote
from adapters.gitremote.gitlab import GitlabRemote
from adapters.gitremote.registry import resolve_remote


def test_parse_remote_url_variants() -> None:
    ref = parse_remote_url("https://github.com/owner/repo.git")
    assert ref is not None and (ref.host, ref.path) == ("github.com", "owner/repo")
    ref = parse_remote_url("https://gitlab.example.com/group/sub/project/")
    assert ref is not None and ref.path == "group/sub/project"
    assert parse_remote_url("git@github.com:owner/repo.git") is None
    assert parse_remote_url("https://host.only/") is None


def test_registry_resolution() -> None:
    assert type(resolve_remote("https://github.com/o/r")) is GithubRemote
    assert type(resolve_remote("https://gitlab.com/g/p")) is GitlabRemote
    assert type(resolve_remote("https://codeberg.org/o/r")) is GiteaRemote
    # Self-hosted without a hint -> generic; hint wins.
    assert type(resolve_remote("https://git.corp.example/o/r")) is GenericRemote
    assert type(resolve_remote("https://git.corp.example/o/r", "gitlab")) is GitlabRemote
    assert resolve_remote(None) is None
    assert resolve_remote("not a url") is None


def test_push_userinfo_conventions() -> None:
    assert GithubRemote().push_userinfo("T") == "x-access-token:T"
    assert GitlabRemote().push_userinfo("T") == "oauth2:T"
    assert GiteaRemote().push_userinfo("T") == "T"
    assert GenericRemote().push_userinfo("T") == "x-access-token:T"


async def test_github_create_flow() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/repos/o/r/pulls" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/repos/o/r" and request.method == "GET":
            return httpx.Response(200, json={"default_branch": "trunk"})
        if request.url.path == "/repos/o/r/pulls" and request.method == "POST":
            body = json.loads(request.content)
            assert body["base"] == "trunk" and body["head"] == "pyr/x-1"
            return httpx.Response(201, json={"number": 7, "html_url": "https://gh/pr/7"})
        raise AssertionError(request.url.path)

    remote = GithubRemote(transport=httpx.MockTransport(handler))
    pr = await remote.ensure_pull_request(
        "https://github.com/o/r", "tok", branch="pyr/x-1", title="t", body="b"
    )
    assert pr is not None and pr.number == 7 and "gh/pr/7" in pr.html_url


async def test_gitlab_subgroup_find_existing_and_note() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Project path must be URL-encoded (subgroups!).
        assert "/projects/grp%2Fsub%2Fproj/" in str(request.url)
        assert request.headers["PRIVATE-TOKEN"] == "tok"
        if "merge_requests?" in str(request.url):
            return httpx.Response(200, json=[{"iid": 3, "web_url": "https://gl/mr/3"}])
        if str(request.url).endswith("/merge_requests/3/notes"):
            return httpx.Response(201, json={})
        raise AssertionError(str(request.url))

    remote = GitlabRemote(transport=httpx.MockTransport(handler))
    url = "https://gitlab.example.com/grp/sub/proj.git"
    pr = await remote.ensure_pull_request(url, "tok", branch="pyr/x-1", title="t", body="b")
    assert pr is not None and pr.number == 3
    assert await remote.post_pr_comment(url, "tok", number=3, body="looks good")


async def test_gitea_find_by_head_and_comment() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "token tok"
        path = request.url.path
        if path == "/api/v1/repos/o/r/pulls" and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {"number": 1, "html_url": "u1", "head": {"ref": "other"}},
                    {"number": 2, "html_url": "https://gt/pr/2", "head": {"ref": "pyr/x-1"}},
                ],
            )
        if path == "/api/v1/repos/o/r/issues/2/comments":
            return httpx.Response(201, json={})
        raise AssertionError(path)

    remote = GiteaRemote(transport=httpx.MockTransport(handler))
    url = "https://gitea.example.com/o/r"
    pr = await remote.ensure_pull_request(url, "tok", branch="pyr/x-1", title="t", body="b")
    assert pr is not None and pr.number == 2
    assert await remote.post_pr_comment(url, "tok", number=2, body="c")


async def test_generic_is_push_only() -> None:
    remote = GenericRemote()
    assert (
        await remote.ensure_pull_request(
            "https://any.example/o/r", "tok", branch="b", title="t", body="b"
        )
        is None
    )
    assert not await remote.post_pr_comment("https://any.example/o/r", "tok", number=1, body="c")


async def test_github_submit_review_and_merge() -> None:
    """The two identity-bearing actions: a formal review event and a merge, each carrying
    the acting bot's token to the right endpoint with the right payload."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        if request.url.path == "/repos/o/r/pulls/7/reviews" and request.method == "POST":
            seen["review"] = json.loads(request.content)
            return httpx.Response(200, json={"id": 1})
        if request.url.path == "/repos/o/r/pulls/7/merge" and request.method == "PUT":
            seen["merge"] = json.loads(request.content)
            return httpx.Response(200, json={"merged": True})
        raise AssertionError(f"{request.method} {request.url.path}")

    remote = GithubRemote(transport=httpx.MockTransport(handler))
    ok = await remote.submit_review(
        "https://github.com/o/r", "REVIEWER_TOKEN", number=7, verdict="approve", body="LGTM"
    )
    assert ok is True
    assert seen["review"] == {"event": "APPROVE", "body": "LGTM"}
    assert seen["auth"] == "Bearer REVIEWER_TOKEN"

    ok = await remote.merge_pull_request(
        "https://github.com/o/r", "MERGER_TOKEN", number=7, method="squash"
    )
    assert ok is True
    assert seen["merge"] == {"merge_method": "squash"}
    assert seen["auth"] == "Bearer MERGER_TOKEN"


async def test_github_merge_gate_refusal_is_false_not_an_error() -> None:
    """A merge the host refuses (e.g. a required review has not landed) returns False --
    the gate doing its job, surfaced as a clean 'did not merge', never a raised error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(405, json={"message": "Required review not approved"})

    remote = GithubRemote(transport=httpx.MockTransport(handler))
    ok = await remote.merge_pull_request("https://github.com/o/r", "T", number=7)
    assert ok is False


async def test_gitlab_review_approve_vs_request_changes() -> None:
    """GitLab has a real approve endpoint; request_changes has no direct action and
    degrades to an MR note rather than failing."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={})

    remote = GitlabRemote(transport=httpx.MockTransport(handler))
    assert await remote.submit_review(
        "https://gitlab.com/g/p", "T", number=3, verdict="approve", body=""
    )
    assert await remote.submit_review(
        "https://gitlab.com/g/p", "T", number=3, verdict="request_changes", body="fix the bug"
    )
    assert paths[0].endswith("/merge_requests/3/approve")
    assert paths[1].endswith("/merge_requests/3/notes")


async def test_generic_remote_has_no_review_or_merge() -> None:
    r = GenericRemote()
    review = await r.submit_review("https://x/o/r", "T", number=1, verdict="approve", body="")
    assert review is False
    assert await r.merge_pull_request("https://x/o/r", "T", number=1) is False


class _MergeTransport(httpx.AsyncBaseTransport):
    """Answers the merge endpoint with a scripted sequence of statuses."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        code = self.statuses.pop(0) if self.statuses else 500
        return httpx.Response(code, json={"message": "scripted"})


async def test_merge_retries_while_github_is_still_computing_mergeability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`405` means "not mergeable" and "not mergeable YET" alike.

    GitHub decides mergeability asynchronously after a push, and a review that approves
    seconds after its own rework lands inside that window. Observed live: the merge was
    refused, and the identical call succeeded minutes later with nothing changed -- so a
    single attempt turns a timing race into a pull request that silently never lands.
    """
    from adapters.gitremote import github as gh

    transport = _MergeTransport([405, 405, 200])
    monkeypatch.setattr(gh, "_MERGE_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(
        gh.GithubRemote, "_client", lambda self, token: httpx.AsyncClient(transport=transport)
    )

    ok = await gh.GithubRemote().merge_pull_request("https://github.com/o/r.git", "tok", number=10)

    assert ok is True
    assert transport.calls == 3


async def test_merge_does_not_retry_a_real_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 403 is an answer, not a race: retrying it just delays the report."""
    from adapters.gitremote import github as gh

    transport = _MergeTransport([403])
    monkeypatch.setattr(gh, "_MERGE_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(
        gh.GithubRemote, "_client", lambda self, token: httpx.AsyncClient(transport=transport)
    )

    ok = await gh.GithubRemote().merge_pull_request("https://github.com/o/r.git", "tok", number=10)

    assert ok is False
    assert transport.calls == 1
