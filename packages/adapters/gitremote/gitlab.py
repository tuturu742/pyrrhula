"""GitLab — gitlab.com AND self-hosted (the API base derives from the remote's own
host). Projects are addressed by URL-encoded full path (supports subgroups); merge
requests stand in for PRs; comments are MR notes. Token: ``PRIVATE-TOKEN`` header (a
personal/project access token); pushes use the ``oauth2:<token>`` userinfo GitLab
documents for token auth over https."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from adapters.gitremote.base import (
    MergeMethod,
    RemotePR,
    RemoteRepoRef,
    ReviewVerdict,
    parse_remote_url,
)


class GitlabRemote:
    key = "gitlab"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def push_userinfo(self, token: str) -> str:
        return f"oauth2:{token}"

    def _client(self, ref: RemoteRepoRef, token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{ref.scheme}://{ref.host}/api/v4",
            timeout=30,
            headers={"PRIVATE-TOKEN": token},
            transport=self._transport,
        )

    async def ensure_pull_request(
        self, source_url: str, token: str, *, branch: str, title: str, body: str
    ) -> RemotePR | None:
        ref = parse_remote_url(source_url)
        if ref is None:
            return None
        project = quote(ref.path, safe="")
        async with self._client(ref, token) as client:
            existing = await client.get(
                f"/projects/{project}/merge_requests",
                params={"source_branch": branch, "state": "opened"},
            )
            if existing.status_code == 200 and existing.json():
                mr = existing.json()[0]
                return RemotePR(number=mr["iid"], html_url=mr["web_url"])

            info = await client.get(f"/projects/{project}")
            info.raise_for_status()
            target = info.json().get("default_branch") or "main"

            created = await client.post(
                f"/projects/{project}/merge_requests",
                json={
                    "source_branch": branch,
                    "target_branch": target,
                    "title": title,
                    "description": body,
                },
            )
            created.raise_for_status()
            mr = created.json()
            return RemotePR(number=mr["iid"], html_url=mr["web_url"])

    async def post_pr_comment(self, source_url: str, token: str, *, number: int, body: str) -> bool:
        ref = parse_remote_url(source_url)
        if ref is None:
            return False
        project = quote(ref.path, safe="")
        try:
            async with self._client(ref, token) as client:
                resp = await client.post(
                    f"/projects/{project}/merge_requests/{number}/notes",
                    json={"body": body},
                )
                return resp.status_code < 300
        except httpx.HTTPError:
            return False

    async def submit_review(
        self, source_url: str, token: str, *, number: int, verdict: ReviewVerdict, body: str
    ) -> bool:
        # GitLab has a first-class *approve* endpoint (the gate branch protection reads),
        # but no direct "request changes" review action -- there it degrades to an MR note,
        # the same graceful fallback the generic remote makes. "comment" is always a note.
        ref = parse_remote_url(source_url)
        if ref is None:
            return False
        project = quote(ref.path, safe="")
        try:
            async with self._client(ref, token) as client:
                if verdict == "approve":
                    resp = await client.post(f"/projects/{project}/merge_requests/{number}/approve")
                    return resp.status_code < 300
                note = body if verdict == "comment" else f"**Changes requested.**\n\n{body}"
                resp = await client.post(
                    f"/projects/{project}/merge_requests/{number}/notes", json={"body": note}
                )
                return resp.status_code < 300
        except httpx.HTTPError:
            return False

    async def merge_pull_request(
        self, source_url: str, token: str, *, number: int, method: MergeMethod = "merge"
    ) -> bool:
        # GitLab's one merge endpoint; squash is a flag rather than a distinct method.
        ref = parse_remote_url(source_url)
        if ref is None:
            return False
        project = quote(ref.path, safe="")
        try:
            async with self._client(ref, token) as client:
                resp = await client.put(
                    f"/projects/{project}/merge_requests/{number}/merge",
                    json={"squash": method == "squash"},
                )
                return resp.status_code < 300
        except httpx.HTTPError:
            return False
