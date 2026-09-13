"""Provider resolution: an explicit hint (the repo registration's ``provider`` column)
always wins; without one, only the big public hosts are auto-detected — a self-hosted
GitLab/Gitea is indistinguishable from any other https remote, so those need the hint
(the repos UI offers the dropdown). Unknown/unhinted remotes get the push-only generic
provider rather than a wrong API guess."""

from __future__ import annotations

from adapters.gitremote.base import GenericRemote, GitRemote, parse_remote_url
from adapters.gitremote.gitea import GiteaRemote
from adapters.gitremote.github import GithubRemote
from adapters.gitremote.gitlab import GitlabRemote

PROVIDER_KEYS = ("github", "gitlab", "gitea", "generic")

_BY_KEY: dict[str, type] = {
    "github": GithubRemote,
    "gitlab": GitlabRemote,
    "gitea": GiteaRemote,
    "generic": GenericRemote,
}

_AUTO_HOSTS = {
    "github.com": "github",
    "www.github.com": "github",
    "gitlab.com": "gitlab",
    "www.gitlab.com": "gitlab",
    "codeberg.org": "gitea",  # Forgejo's flagship host
}


def resolve_remote(source_url: str | None, provider_hint: str | None = None) -> GitRemote | None:
    """The provider for a registered remote, or None when there is no usable https
    remote at all (no URL / unparseable)."""
    if not source_url:
        return None
    ref = parse_remote_url(source_url)
    if ref is None:
        return None
    key = (provider_hint or "").strip().lower()
    if key in _BY_KEY:
        hinted: GitRemote = _BY_KEY[key]()
        return hinted
    detected = _AUTO_HOSTS.get(ref.host.lower())
    if detected:
        matched: GitRemote = _BY_KEY[detected]()
        return matched
    return GenericRemote()
