"""The git remote provider seam: everything provider-specific about talking to a
hosted git service (PR creation, PR comments, the push-URL credential convention) lives
behind ``GitRemote``. Pushing itself is plain git and stays in ``GitStore``; these
adapters only shape the URL userinfo and drive the provider's REST API.

Everything is best-effort from the delegation flow's point of view: the branch push is
the ground truth; a failed PR call degrades to the internal PR record, never a failed
delegation. API responses are data; only id/url fields are consumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlsplit

# A reviewer's verdict on a PR/MR. "approve" and "request_changes" are FORMAL reviews the
# host's merge gate can enforce; "comment" is a non-blocking note. Delegation threads the
# ACTING persona's own token to each call, so a formal approve comes from a reviewer
# identity distinct from the PR author -- the thing a single platform token cannot do.
ReviewVerdict = Literal["approve", "request_changes", "comment"]
MergeMethod = Literal["merge", "squash", "rebase"]


@dataclass(frozen=True)
class RemotePR:
    number: int
    html_url: str


# What became of a pull request, as the host sees it. "open" and "merged" are
# self-explanatory; "closed" means closed WITHOUT merging, which is the case the
# delegation lifecycle had no answer for -- the work item sat in review forever because
# nothing told it the pull request was never going to land.
PRState = Literal["open", "merged", "closed"]


@dataclass(frozen=True)
class RemotePRStatus:
    number: int
    state: PRState


@dataclass(frozen=True)
class RemoteRepoRef:
    """A parsed https remote: host + the repo path (owner/name, or a GitLab
    group/subgroup/project path), without a trailing ``.git``."""

    scheme: str
    host: str
    path: str  # no leading/trailing slash


def parse_remote_url(source_url: str) -> RemoteRepoRef | None:
    parts = urlsplit(source_url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    path = parts.path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not path or "/" not in path:
        return None
    return RemoteRepoRef(scheme=parts.scheme, host=parts.netloc, path=path)


class GitRemote(Protocol):
    """One hosted-git provider. ``token`` is the repo registration's decrypted access
    token; it is used transiently per call and never persisted or logged."""

    def push_userinfo(self, token: str) -> str:
        """The ``user:pass`` userinfo the provider expects in an authenticated https
        push URL (e.g. GitHub ``x-access-token:<t>``, GitLab ``oauth2:<t>``)."""
        ...

    async def ensure_pull_request(
        self, source_url: str, token: str, *, branch: str, title: str, body: str
    ) -> RemotePR | None:
        """Create the PR/MR for ``branch`` against the default branch, or return the
        existing open one. None when the URL doesn't parse for this provider."""
        ...

    async def post_pr_comment(self, source_url: str, token: str, *, number: int, body: str) -> bool:
        """Post one comment on the PR/MR. False on any failure (best-effort)."""
        ...

    async def submit_review(
        self, source_url: str, token: str, *, number: int, verdict: ReviewVerdict, body: str
    ) -> bool:
        """File a FORMAL review (approve / request changes / comment) under ``token``'s
        identity. False on any failure, and False for providers with no review API (a
        caller wanting the verdict recorded anyway falls back to ``post_pr_comment``)."""
        ...

    async def merge_pull_request(
        self, source_url: str, token: str, *, number: int, method: MergeMethod = "merge"
    ) -> bool:
        """Merge the PR/MR under ``token``'s identity. False on any failure -- including a
        host merge gate refusing because a required review has not landed, which is the
        gate doing its job, not an error to paper over."""
        ...

    async def pull_request_status(
        self, source_url: str, token: str, *, number: int
    ) -> RemotePRStatus | None:
        """What the host says became of this PR/MR. ``None`` when it cannot be
        determined -- the URL does not parse, the provider has no PR API, the request
        failed. None means "unknown", never "gone": a caller must not conclude anything
        from it, because concluding "closed" from a network error would abandon live
        work."""
        ...


class GenericRemote:
    """Push-only: any https git remote. No PR API, no comments — the branch push is the
    whole integration. The GitHub-style userinfo is the most widely accepted default
    for token pushes (many servers ignore the username entirely)."""

    key = "generic"

    def push_userinfo(self, token: str) -> str:
        return f"x-access-token:{token}"

    async def ensure_pull_request(
        self, source_url: str, token: str, *, branch: str, title: str, body: str
    ) -> RemotePR | None:
        return None

    async def post_pr_comment(self, source_url: str, token: str, *, number: int, body: str) -> bool:
        return False

    async def submit_review(
        self, source_url: str, token: str, *, number: int, verdict: ReviewVerdict, body: str
    ) -> bool:
        return False

    async def merge_pull_request(
        self, source_url: str, token: str, *, number: int, method: MergeMethod = "merge"
    ) -> bool:
        return False

    async def pull_request_status(
        self, source_url: str, token: str, *, number: int
    ) -> RemotePRStatus | None:
        return None
