"""Gitea / Forgejo — self-hosted (API base derives from the remote's host). A GitHub-
shaped API: /repos/{owner}/{repo}/pulls, comments via the issues API. Token: the
``Authorization: token <t>`` header; pushes accept the token as the URL username."""

from __future__ import annotations

import httpx

from adapters.gitremote.base import (
    MergeMethod,
    RemotePR,
    RemoteRepoRef,
    ReviewVerdict,
    parse_remote_url,
)

_REVIEW_EVENT = {"approve": "APPROVED", "request_changes": "REQUEST_CHANGES", "comment": "COMMENT"}
_MERGE_DO = {"merge": "merge", "squash": "squash", "rebase": "rebase"}


class GiteaRemote:
    key = "gitea"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def push_userinfo(self, token: str) -> str:
        # Gitea/Forgejo accept the token as the basic-auth username with any password;
        # token-as-username with an empty password is the documented convention.
        return token

    def _split(self, source_url: str) -> tuple[RemoteRepoRef, str, str] | None:
        ref = parse_remote_url(source_url)
        if ref is None or ref.path.count("/") != 1:
            return None
        owner, repo = ref.path.split("/")
        return ref, owner, repo

    def _client(self, ref: RemoteRepoRef, token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{ref.scheme}://{ref.host}/api/v1",
            timeout=30,
            headers={"Authorization": f"token {token}"},
            transport=self._transport,
        )

    async def ensure_pull_request(
        self, source_url: str, token: str, *, branch: str, title: str, body: str
    ) -> RemotePR | None:
        parsed = self._split(source_url)
        if parsed is None:
            return None
        ref, owner, repo = parsed
        async with self._client(ref, token) as client:
            existing = await client.get(f"/repos/{owner}/{repo}/pulls", params={"state": "open"})
            if existing.status_code == 200:
                for pr in existing.json():
                    if (pr.get("head") or {}).get("ref") == branch:
                        return RemotePR(number=pr["number"], html_url=pr["html_url"])

            info = await client.get(f"/repos/{owner}/{repo}")
            info.raise_for_status()
            base = info.json().get("default_branch") or "main"

            created = await client.post(
                f"/repos/{owner}/{repo}/pulls",
                json={"title": title, "head": branch, "base": base, "body": body},
            )
            created.raise_for_status()
            pr = created.json()
            return RemotePR(number=pr["number"], html_url=pr["html_url"])

    async def post_pr_comment(self, source_url: str, token: str, *, number: int, body: str) -> bool:
        parsed = self._split(source_url)
        if parsed is None:
            return False
        ref, owner, repo = parsed
        try:
            async with self._client(ref, token) as client:
                resp = await client.post(
                    f"/repos/{owner}/{repo}/issues/{number}/comments",
                    json={"body": body},
                )
                return resp.status_code < 300
        except httpx.HTTPError:
            return False

    async def submit_review(
        self, source_url: str, token: str, *, number: int, verdict: ReviewVerdict, body: str
    ) -> bool:
        parsed = self._split(source_url)
        if parsed is None:
            return False
        ref, owner, repo = parsed
        try:
            async with self._client(ref, token) as client:
                resp = await client.post(
                    f"/repos/{owner}/{repo}/pulls/{number}/reviews",
                    json={"event": _REVIEW_EVENT[verdict], "body": body},
                )
                return resp.status_code < 300
        except httpx.HTTPError:
            return False

    async def merge_pull_request(
        self, source_url: str, token: str, *, number: int, method: MergeMethod = "merge"
    ) -> bool:
        parsed = self._split(source_url)
        if parsed is None:
            return False
        ref, owner, repo = parsed
        try:
            async with self._client(ref, token) as client:
                resp = await client.post(
                    f"/repos/{owner}/{repo}/pulls/{number}/merge",
                    json={"Do": _MERGE_DO[method]},
                )
                return resp.status_code < 300
        except httpx.HTTPError:
            return False
