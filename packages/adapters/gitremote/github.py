"""GitHub (github.com). Moved from ``adapters/mcp/github_remote.py`` behaviorally
unchanged: find-open-PR by head, else create against the repo's default branch;
comments via the issues API (works regardless of who authored the PR; GitHub refuses
formal reviews from the PR's own author, which the platform token is)."""

from __future__ import annotations

import httpx

from adapters.gitremote.base import MergeMethod, RemotePR, ReviewVerdict, parse_remote_url

_API = "https://api.github.com"

# verdict -> the GitHub review `event`; the merge_method GitHub's merge endpoint expects.
_REVIEW_EVENT = {"approve": "APPROVE", "request_changes": "REQUEST_CHANGES", "comment": "COMMENT"}
_MERGE_METHOD = {"merge": "merge", "squash": "squash", "rebase": "rebase"}


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


class GithubRemote:
    key = "github"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _client(self, token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=30, headers=_headers(token), transport=self._transport)

    def push_userinfo(self, token: str) -> str:
        return f"x-access-token:{token}"

    def _owner_repo(self, source_url: str) -> tuple[str, str] | None:
        ref = parse_remote_url(source_url)
        if ref is None or ref.path.count("/") != 1:
            return None
        owner, repo = ref.path.split("/")
        return owner, repo

    async def ensure_pull_request(
        self, source_url: str, token: str, *, branch: str, title: str, body: str
    ) -> RemotePR | None:
        parsed = self._owner_repo(source_url)
        if parsed is None:
            return None
        owner, repo = parsed
        async with self._client(token) as client:
            existing = await client.get(
                f"{_API}/repos/{owner}/{repo}/pulls",
                params={"head": f"{owner}:{branch}", "state": "open"},
            )
            if existing.status_code == 200 and existing.json():
                pr = existing.json()[0]
                return RemotePR(number=pr["number"], html_url=pr["html_url"])

            info = await client.get(f"{_API}/repos/{owner}/{repo}")
            info.raise_for_status()
            base = info.json().get("default_branch") or "main"

            created = await client.post(
                f"{_API}/repos/{owner}/{repo}/pulls",
                json={"title": title, "head": branch, "base": base, "body": body},
            )
            created.raise_for_status()
            pr = created.json()
            return RemotePR(number=pr["number"], html_url=pr["html_url"])

    async def post_pr_comment(self, source_url: str, token: str, *, number: int, body: str) -> bool:
        parsed = self._owner_repo(source_url)
        if parsed is None:
            return False
        owner, repo = parsed
        try:
            async with self._client(token) as client:
                resp = await client.post(
                    f"{_API}/repos/{owner}/{repo}/issues/{number}/comments",
                    json={"body": body},
                )
                return resp.status_code < 300
        except httpx.HTTPError:
            return False

    async def submit_review(
        self, source_url: str, token: str, *, number: int, verdict: ReviewVerdict, body: str
    ) -> bool:
        parsed = self._owner_repo(source_url)
        if parsed is None:
            return False
        owner, repo = parsed
        try:
            async with self._client(token) as client:
                resp = await client.post(
                    f"{_API}/repos/{owner}/{repo}/pulls/{number}/reviews",
                    json={"event": _REVIEW_EVENT[verdict], "body": body},
                )
                return resp.status_code < 300
        except httpx.HTTPError:
            return False

    async def merge_pull_request(
        self, source_url: str, token: str, *, number: int, method: MergeMethod = "merge"
    ) -> bool:
        parsed = self._owner_repo(source_url)
        if parsed is None:
            return False
        owner, repo = parsed
        try:
            async with self._client(token) as client:
                resp = await client.put(
                    f"{_API}/repos/{owner}/{repo}/pulls/{number}/merge",
                    json={"merge_method": _MERGE_METHOD[method]},
                )
                return resp.status_code < 300
        except httpx.HTTPError:
            return False
