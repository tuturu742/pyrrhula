"""GitHub (github.com). Moved from ``adapters/mcp/github_remote.py`` behaviorally
unchanged: find-open-PR by head, else create against the repo's default branch;
comments via the issues API (works regardless of who authored the PR; GitHub refuses
formal reviews from the PR's own author, which the platform token is)."""

from __future__ import annotations

import asyncio

import httpx

from adapters.gitremote.base import (
    MergeMethod,
    RemotePR,
    RemotePRStatus,
    ReviewVerdict,
    parse_remote_url,
)

# GitHub decides mergeability asynchronously; these cover that window without
# turning a genuine refusal into a long wait.
_MERGE_ATTEMPTS = 4
_MERGE_RETRY_SECONDS = 3.0
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
                for attempt in range(_MERGE_ATTEMPTS):
                    resp = await client.put(
                        f"{_API}/repos/{owner}/{repo}/pulls/{number}/merge",
                        json={"merge_method": _MERGE_METHOD[method]},
                    )
                    if resp.status_code < 300:
                        return True
                    # "Not mergeable" and "not mergeable YET" arrive as the same 405.
                    # GitHub computes mergeability asynchronously after a push, and a
                    # review that approves seconds after its own rework lands inside
                    # that window -- observed here: the merge was refused, then the
                    # identical call succeeded minutes later with nothing changed.
                    # Worth a few seconds before believing the refusal.
                    if resp.status_code != 405 or attempt == _MERGE_ATTEMPTS - 1:
                        break
                    await asyncio.sleep(_MERGE_RETRY_SECONDS * (attempt + 1))
                # The refusal is the only useful thing here and it was being thrown
                # away: a False told the caller "not merged" and nothing told anyone
                # why -- a token without merge permission, a required check still
                # pending, a protected branch, and an outage all looked identical, and
                # answering "why did it not merge?" meant reproducing the call by hand
                # against the API. GitHub says which; this repeats it.
                import structlog

                structlog.get_logger().warning(
                    "gitremote.merge_refused",
                    attempts=_MERGE_ATTEMPTS if resp.status_code == 405 else 1,
                    provider="github",
                    number=number,
                    status=resp.status_code,
                    reason=resp.text[:300],
                )
                return False
        except httpx.HTTPError as exc:
            import structlog

            structlog.get_logger().warning(
                "gitremote.merge_failed", provider="github", number=number, error=str(exc)[:200]
            )
            return False

    async def pull_request_status(
        self, source_url: str, token: str, *, number: int
    ) -> RemotePRStatus | None:
        parsed = self._owner_repo(source_url)
        if parsed is None:
            return None
        owner, repo = parsed
        try:
            async with self._client(token) as client:
                resp = await client.get(f"{_API}/repos/{owner}/{repo}/pulls/{number}")
                if resp.status_code >= 300:
                    return None
                pr = resp.json()
        except httpx.HTTPError:
            return None
        # `state` is only "open" or "closed"; a merged PR is closed AND carries
        # merged_at. Reading state alone would report every merge as an abandonment.
        if pr.get("merged_at"):
            return RemotePRStatus(number=number, state="merged")
        if pr.get("state") == "closed":
            return RemotePRStatus(number=number, state="closed")
        return RemotePRStatus(number=number, state="open")
