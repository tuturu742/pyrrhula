"""Server-side git store for delegated coding work (D15).

The hosted-service model keeps *all* branching/editing/PRs on the service side -- never on a
client's local checkout. This is that store: real git repositories under a volume-backed root
(one dir per repo), driven with the ``git`` binary. Branches are real branches; a "pull request"
is a record in a JSON sidecar (``prs.json``) keyed by branch -- enough for the delegation flow
(open a PR, look it up, add review-fix commits) without standing up a full forge.

Parallel-safe: an in-process per-repo lock serialises the working-tree operations, so several
delegated work items committing to *different* branches of the same repo don't race on the
shared checkout. (A multi-worker deployment would swap this for a filesystem lock; single-worker
today.)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import shutil
from collections import defaultdict
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Pyrrhula",
    "GIT_AUTHOR_EMAIL": "agent@pyrrhula.local",
    "GIT_COMMITTER_NAME": "Pyrrhula",
    "GIT_COMMITTER_EMAIL": "agent@pyrrhula.local",
    "GIT_TERMINAL_PROMPT": "0",
    # api (uid 10001) and worker (root, for the exec-env socket) share the store volume;
    # git's dubious-ownership guard would otherwise refuse cross-uid repos. The store is a
    # single-service backend directory, not a user checkout -- waiving the check here is
    # scoped to exactly these subprocess invocations.
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.directory",
    "GIT_CONFIG_VALUE_0": "*",
}


def default_git_root() -> str:
    """The store's root, shared by the api (repo registration) and worker (delegation)
    composition roots. Under the blobs volume so all VCS state survives container
    recreates -- the hosted-service model keeps it server-side, never on a client."""
    return os.environ.get("PYRRHULA_MCP_GIT_ROOT", "/app/data/blobs/repos")


def authed_url(url: str, token: str | None, userinfo: str | None = None) -> str:
    """An https URL with credentials in the userinfo slot. The userinfo convention is
    provider-specific (GitHub 'x-access-token:<t>', GitLab 'oauth2:<t>', Gitea token-as-
    username) -- callers pass the provider's own via ``userinfo``; the GitHub-style
    default keeps every existing call site byte-identical. Command-line only, never
    persisted; git redacts credentials from its own stderr."""
    if not token:
        return url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or "@" in parts.netloc:
        return url
    info = userinfo if userinfo is not None else f"x-access-token:{token}"
    return urlunsplit(parts._replace(netloc=f"{info}@{parts.netloc}"))


class GitStoreError(Exception):
    pass


class GitStore:
    def __init__(self, root: str) -> None:
        self._root = pathlib.Path(root)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _repo_dir(self, repo_key: str) -> pathlib.Path:
        return self._root / repo_key

    def _work(self, repo_key: str) -> pathlib.Path:
        return self._repo_dir(repo_key) / "repo"

    def _prs_path(self, repo_key: str) -> pathlib.Path:
        return self._repo_dir(repo_key) / "prs.json"

    async def _git(self, repo_key: str, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self._work(repo_key)),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", **_GIT_ENV},
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise GitStoreError(f"git {' '.join(args)} failed: {err.decode()[:300]}")
        return out.decode()

    def _sweep_broken_refs(self, repo_key: str) -> None:
        """Remove zero-length loose ref files. A process killed mid-ref-write (observed
        live: a container kill during a push) leaves an empty file that git advertises
        as an all-zeros ref -- which then fails EVERY subsequent clone/fetch with
        'not our ref 0000...'. One corrupt ref must never brick the whole repo."""
        refs = self._work(repo_key) / ".git" / "refs"
        if not refs.is_dir():
            return
        for path in refs.rglob("*"):
            if path.is_file() and path.stat().st_size == 0:
                path.unlink(missing_ok=True)

    @staticmethod
    def _write_file(dest: pathlib.Path, content: str | bytes) -> None:
        """Text or binary. Generated assets (a sprite from an image model) arrive as
        bytes, and ``write_text`` on those would corrupt them silently -- a PNG is not
        valid UTF-8, so it would raise, and any encode-with-errors workaround would
        produce a file that is no longer an image."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            dest.write_bytes(content)
        else:
            dest.write_text(content)

    async def ensure_repo(
        self, repo_key: str, *, seed_files: Mapping[str, str | bytes] | None = None
    ) -> None:
        """Create the repo (with an initial commit on ``main``) if it does not exist yet."""
        async with self._locks[repo_key]:
            work = self._work(repo_key)
            if (work / ".git").exists():
                self._sweep_broken_refs(repo_key)
                return
            work.mkdir(parents=True, exist_ok=True)
            await self._git(repo_key, "init", "-q", "-b", "main")
            files: Mapping[str, str | bytes] = seed_files or {"README.md": f"# {repo_key}\n"}
            for rel, content in files.items():
                self._write_file(work / rel, content)
            await self._git(repo_key, "add", "-A")
            await self._git(repo_key, "commit", "-q", "-m", "Initial commit")
            self._prs_path(repo_key).write_text("{}")

    async def clone_from(
        self,
        repo_key: str,
        url: str,
        *,
        token: str | None = None,
        userinfo: str | None = None,
    ) -> None:
        """Import a repo's starting content by cloning ``url`` into the store. A token (for
        private https remotes) is injected as userinfo on the clone URL only -- never stored,
        never logged (GitStoreError carries git's stderr, which redacts credentials itself).
        ``file://`` sources work with no network. Raises GitStoreError on failure; the caller
        treats import as best-effort (the repo stays registered, just empty)."""
        clone_url = authed_url(url, token, userinfo)
        async with self._locks[repo_key]:
            work = self._work(repo_key)
            if (work / ".git").exists():
                raise GitStoreError(f"repo {repo_key!r} already has content; not overwriting")
            work.parent.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                "-q",
                clone_url,
                str(work),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin", **_GIT_ENV},
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                shutil.rmtree(work, ignore_errors=True)
                raise GitStoreError(f"clone failed: {err.decode()[:300]}")
            # Normalize the primary branch name so branch/PR conventions hold store-wide.
            head = await self._git(repo_key, "rev-parse", "--abbrev-ref", "HEAD")
            if head.strip() != "main":
                await self._git(repo_key, "branch", "-m", head.strip(), "main")
            if not self._prs_path(repo_key).exists():
                self._prs_path(repo_key).write_text("{}")

    async def import_tree(self, repo_key: str, files: Mapping[str, str | bytes]) -> None:
        """Seed a repo's contents from an in-memory file map (e.g. an imported project) and
        commit on ``main``. No-op for files already identical."""
        async with self._locks[repo_key]:
            await self._git(repo_key, "checkout", "-q", "main")
            work = self._work(repo_key)
            for rel, content in files.items():
                self._write_file(work / rel, content)
            await self._git(repo_key, "add", "-A")
            status = await self._git(repo_key, "status", "--porcelain")
            if status.strip():
                await self._git(repo_key, "commit", "-q", "-m", "Import project seed")

    async def commit_on_branch(
        self,
        repo_key: str,
        branch: str,
        files: Mapping[str, str | bytes],
        message: str,
        *,
        base: str = "main",
    ) -> str:
        """Create ``branch`` off ``base`` (or reuse it if it exists -- the review-fix case),
        write ``files``, commit, and return the new commit sha."""
        async with self._locks[repo_key]:
            existing = await self._git(repo_key, "branch", "--list", branch)
            if existing.strip():
                await self._git(repo_key, "checkout", "-q", branch)
            else:
                await self._git(repo_key, "checkout", "-q", "-b", branch, base)
            work = self._work(repo_key)
            for rel, content in files.items():
                self._write_file(work / rel, content)
            await self._git(repo_key, "add", "-A")
            await self._git(repo_key, "commit", "-q", "-m", message)
            sha = (await self._git(repo_key, "rev-parse", "HEAD")).strip()
            await self._git(repo_key, "checkout", "-q", "main")
            return sha

    async def read_tree(
        self,
        repo_key: str,
        *,
        ref: str = "main",
        max_file_bytes: int = 20_000,
        max_files: int = 40,
    ) -> dict[str, str]:
        """Small text files at ``ref`` as {path: content} -- codegen context. Binary or
        oversized files are listed with empty content (the path still informs the model)."""
        async with self._locks[repo_key]:
            listing = await self._git(repo_key, "ls-tree", "-r", "--name-only", ref)
            files: dict[str, str] = {}
            for path in listing.splitlines()[:max_files]:
                path = path.strip()
                if not path:
                    continue
                try:
                    content = await self._git(repo_key, "show", f"{ref}:{path}")
                except GitStoreError:
                    files[path] = ""
                    continue
                files[path] = content if len(content) <= max_file_bytes else ""
            return files

    async def push_branch(
        self,
        repo_key: str,
        branch: str,
        remote_url: str,
        *,
        token: str | None = None,
        userinfo: str | None = None,
    ) -> None:
        """Push one branch to an external remote (the repo registration's source_url) --
        the server-side counterpart of the one-way import. The token rides the command's
        URL only (https userinfo), never git config, never a stored argument; git redacts
        credentials from its own stderr. force-with-lease semantics are unnecessary: pyr/*
        branches have a single writer (the delegation flow)."""
        push_url = authed_url(remote_url, token, userinfo)
        async with self._locks[repo_key]:
            await self._git(repo_key, "push", "-q", push_url, f"{branch}:{branch}")

    async def branch_exists(self, repo_key: str, branch: str) -> bool:
        if not (self._work(repo_key) / ".git").exists():
            return False
        async with self._locks[repo_key]:
            out = await self._git(repo_key, "branch", "--list", branch)
        return bool(out.strip())

    async def diff_stat(self, repo_key: str, branch: str, base: str = "main") -> dict[str, int]:
        """{files, insertions, deletions} for base...branch — the transcript's one-line
        'what did this PR touch' summary."""
        async with self._locks[repo_key]:
            out = await self._git(repo_key, "diff", "--shortstat", f"{base}...{branch}")
        stats = {"files": 0, "insertions": 0, "deletions": 0}
        for part in out.strip().split(","):
            part = part.strip()
            if not part:
                continue
            count = int(part.split()[0])
            if "file" in part:
                stats["files"] = count
            elif "insertion" in part:
                stats["insertions"] = count
            elif "deletion" in part:
                stats["deletions"] = count
        return stats

    async def diff_text(
        self, repo_key: str, branch: str, base: str = "main", max_chars: int = 40_000
    ) -> str:
        """The unified diff for base...branch, capped — reviewer-model input."""
        async with self._locks[repo_key]:
            out = await self._git(repo_key, "diff", f"{base}...{branch}")
        if len(out) > max_chars:
            return out[:max_chars] + "\n... (diff truncated)"
        return out

    async def head_sha(self, repo_key: str, ref: str = "main") -> str:
        async with self._locks[repo_key]:
            out = await self._git(repo_key, "rev-parse", ref)
        return out.strip()

    async def commit_count(self, repo_key: str, branch: str) -> int:
        async with self._locks[repo_key]:
            out = await self._git(repo_key, "rev-list", "--count", branch)
        return int(out.strip() or "0")

    async def merge_branch(self, repo_key: str, branch: str, *, into: str = "main") -> str:
        """Land a work branch on ``into``. Returns the resulting head sha.

        Delegated work opens a branch per work item, each cut from ``main`` -- so until
        one lands, the next work item cannot see it. Without this, a second work item
        that builds on the first starts from the *unmodified* base and fails against
        code its predecessor already wrote, which reads like the agent regressed when
        nothing of the sort happened.

        A conflict leaves the merge aborted and raises, rather than committing markers:
        an unresolvable overlap is a human's call, not something to paper over."""
        async with self._locks[repo_key]:
            await self._git(repo_key, "checkout", "-q", into)
            try:
                await self._git(repo_key, "merge", "--no-edit", "-q", branch)
            except GitStoreError:
                with contextlib.suppress(GitStoreError):
                    await self._git(repo_key, "merge", "--abort")
                raise
            out = await self._git(repo_key, "rev-parse", "HEAD")
        return out.strip()

    def _load_prs(self, repo_key: str) -> dict[str, dict[str, Any]]:
        path = self._prs_path(repo_key)
        if not path.exists():
            return {}
        loaded: dict[str, dict[str, Any]] = json.loads(path.read_text())
        return loaded

    async def record_pr(self, repo_key: str, branch: str, record: dict[str, Any]) -> None:
        """Merge, never replace: several writers annotate one PR record (the transport's
        dispatch status, the delegation's assignee stamp, the facilitator's reviews) --
        a rework's re-dispatch must not wipe the assignee or the review history."""
        async with self._locks[repo_key]:
            prs = self._load_prs(repo_key)
            prs[branch] = {**prs.get(branch, {}), **record}
            self._prs_path(repo_key).write_text(json.dumps(prs, indent=2))

    async def get_pr(self, repo_key: str, branch: str) -> dict[str, Any] | None:
        async with self._locks[repo_key]:
            return self._load_prs(repo_key).get(branch)

    async def next_pr_number(self, repo_key: str) -> int:
        async with self._locks[repo_key]:
            return len(self._load_prs(repo_key)) + 1

    async def reset(self, repo_key: str) -> None:
        """Drop a repo entirely (test/verification convenience)."""
        shutil.rmtree(self._repo_dir(repo_key), ignore_errors=True)
