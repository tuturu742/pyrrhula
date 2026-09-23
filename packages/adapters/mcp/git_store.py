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
import re
import shutil
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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


_CREDENTIALED_URL_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+@")


def redact_credentials(text: str) -> str:
    """Strip userinfo from any URL in ``text``.

    ``authed_url`` puts a live token in the userinfo slot because that is the only way to
    hand git a credential without persisting one. git redacts credentials from its *own*
    stderr, which is what made this look safe -- but a failure message composed here
    echoes the argv we passed, and the token is in the argv. A push to a repository the
    token could not write logged the whole PAT in plaintext.
    """
    return _CREDENTIALED_URL_RE.sub(r"\g<scheme>***@", text)


@dataclass(frozen=True)
class FetchResult:
    """What a refresh did, said precisely enough to show a user.

    ``status`` is one of:

    * ``unchanged`` -- the store already had the remote's tip.
    * ``fast_forwarded`` -- the remote had moved on and we caught up cleanly.
    * ``ahead`` -- the store is in front of the remote. Normal here rather than
      exceptional: an approved pull request merges into ``main`` *server-side*
      (``merge_branch``), so the store legitimately leads until something pushes out.
    * ``diverged`` -- both moved. Nothing is done, deliberately.
    """

    status: str
    before_sha: str
    after_sha: str
    behind: int = 0
    ahead: int = 0


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
            raise GitStoreError(
                redact_credentials(f"git {' '.join(args)} failed: {err.decode()[:300]}")
            )
        return out.decode()

    async def _git_bytes(self, repo_key: str, *args: str) -> bytes:
        """``_git`` without the decode, for commands whose output may not be text.

        ``git show`` on a binary blob succeeds and returns bytes that are not UTF-8;
        decoding them raised ``UnicodeDecodeError``, which is not a ``GitStoreError``
        and so escaped every caller's handling. One PNG in a repository killed the whole
        workspace analysis job.
        """
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
            raise GitStoreError(
                redact_credentials(f"git {' '.join(args)} failed: {err.decode()[:300]}")
            )
        return out

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
    ) -> str:
        """Import a repo's starting content by cloning ``url`` into the store.

        Returns the name of the branch that was imported, which the caller stores on the
        repository row: it is the remote's own name for it, not an assumption.

        A token (for private https remotes) is injected as userinfo on the clone URL.
        That was described here as "never stored" and it was not true: ``git clone``
        writes the URL it was given into ``.git/config`` as ``origin``, so every
        imported repository kept a live credential in plaintext on the blobs volume --
        readable by anything that can reach the volume, surviving container recreates,
        and riding along in any backup. Found on a live deployment with five
        repositories, every one of them holding a writable token.

        So the remote is rewritten to the bare URL immediately after cloning. The
        credential still has to reach git somehow -- it goes on the command line for the
        length of one subprocess, which is the narrowest window available without a
        credential helper -- but nothing persists it.

        ``file://`` sources work with no network. Raises GitStoreError on failure; the
        caller treats import as best-effort (the repo stays registered, just empty).
        """
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
                raise GitStoreError(redact_credentials(f"clone failed: {err.decode()[:300]}"))
            await self._strip_remote_credentials(repo_key, url)
            # The imported branch keeps its own name. This used to rename it to `main`
            # "so branch/PR conventions hold store-wide", which made the store tidy and
            # threw away the one fact every outbound operation needs -- a repository
            # whose remote calls it `master` could then never be refreshed, and said
            # only "couldn't find remote ref main". The caller records the name instead.
            head = (await self._git(repo_key, "rev-parse", "--abbrev-ref", "HEAD")).strip()
            if not self._prs_path(repo_key).exists():
                self._prs_path(repo_key).write_text("{}")
            return head

    async def _strip_remote_credentials(self, repo_key: str, clean_url: str) -> None:
        """Rewrite ``origin`` to a URL with no userinfo, or drop it if there is none.

        Called straight after clone. Defensive rather than clever: if anything about
        this fails, the repository is usable but would still hold a credential, so it
        raises rather than leaving that quietly true.
        """
        work = self._work(repo_key)
        if not (work / ".git").exists():
            return
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(work),
            "remote",
            "set-url",
            "origin",
            clean_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", **_GIT_ENV},
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise GitStoreError(
                redact_credentials(f"could not clear stored credentials: {err.decode()[:200]}")
            )

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
        deletes: Iterable[str] = (),
    ) -> str:
        """Create ``branch`` off ``base`` (or reuse it if it exists -- the review-fix case),
        write ``files``, remove ``deletes``, commit, and return the new commit sha.

        Deletion is explicit because ``git add -A`` stages removals but nothing was ever
        removing anything: the protocol could only write, so "delete the dead module"
        committed nothing and opened an empty pull request.
        """
        async with self._locks[repo_key]:
            existing = await self._git(repo_key, "branch", "--list", branch)
            if existing.strip():
                await self._git(repo_key, "checkout", "-q", branch)
            else:
                await self._git(repo_key, "checkout", "-q", "-b", branch, base)
            work = self._work(repo_key)
            for rel, content in files.items():
                self._write_file(work / rel, content)
            for rel in deletes:
                target = (work / rel).resolve()
                # Confined to the checkout. The path already passed the codegen parser's
                # traversal check, but this is the call that actually removes things, so
                # it does not take that on trust.
                if not target.is_relative_to(work.resolve()) or target == work.resolve():
                    continue
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            await self._git(repo_key, "add", "-A")
            try:
                await self._git(repo_key, "commit", "-q", "-m", message)
            except GitStoreError:
                # Nothing to commit. The in-container path has always allowed this and
                # this one did not, so a delegation that produced no applicable change
                # died here with git's bare "" instead of opening a pull request that
                # honestly shows no changes. An empty commit is a reviewable answer;
                # an opaque failure is not.
                await self._git(repo_key, "commit", "-q", "--allow-empty", "-m", message)
            sha = (await self._git(repo_key, "rev-parse", "HEAD")).strip()
            # Back to the repository's own base, not a constant: `main` may not exist.
            await self._git(repo_key, "checkout", "-q", base)
            return sha

    async def read_tree(
        self,
        repo_key: str,
        *,
        ref: str = "main",
        max_file_bytes: int = 20_000,
        max_files: int = 40,
        max_paths: int = 4000,
        prefer: Sequence[str] = (),
        prefer_file_bytes: int = 64_000,
    ) -> dict[str, str]:
        """Small text files at ``ref`` as {path: content} -- codegen context. Binary or
        oversized files are listed with empty content (the path still informs the model).

        That contract is now actually kept. It used to be a docstring only: a binary blob
        makes ``git show`` *succeed*, so the failure arrived as ``UnicodeDecodeError``
        from the decode rather than as the ``GitStoreError`` this caught, and a single
        non-UTF-8 file failed the caller's entire job. Read as bytes, size-check as
        bytes, and decode defensively -- "binary" is then a property we observe rather
        than one we hope not to meet.
        """
        async with self._locks[repo_key]:
            listing = await self._git(repo_key, "ls-tree", "-r", "--name-only", ref)
            paths = [p.strip() for p in listing.splitlines()[:max_paths] if p.strip()]

            # The content budget goes to the files the task names, before it goes to
            # whatever sorts first. Alphabetical order is not a relevance ranking: asked
            # to update five snapshot fixtures, the agent was handed the first forty
            # paths of a Rust workspace and answered, correctly, that the files it had
            # been asked about "were given as" empty -- then fell back to a placeholder,
            # and a reviewer rejected a pull request whose real fault was the context.
            preferred: set[str] = set()
            if prefer:
                wanted = [p for p in paths if any(token and token in p for token in prefer)]
                preferred = set(wanted)
                rest = [p for p in paths if p not in preferred]
                paths = wanted + rest

            files: dict[str, str] = {}
            for index, path in enumerate(paths):
                # Past the content budget the path is still reported, with no body. A
                # path costs a line; a body costs the prompt. Truncating the *listing*
                # to the content budget made everything past it invisible -- on a 1088
                # file repository the agent saw 150 paths, alphabetically, and could not
                # act on anything below `m`: asked to delete `tasks/`, it correctly
                # concluded there was no such thing and did nothing.
                if index >= max_files:
                    files[path] = ""
                    continue
                try:
                    raw = await self._git_bytes(repo_key, "show", f"{ref}:{path}")
                except GitStoreError:
                    files[path] = ""
                    continue
                # A file the task named gets a larger allowance than the bulk fill.
                # One of the five snapshot fixtures an agent was asked to update is
                # 26KB: under the ordinary budget it arrived empty, named in the task
                # and unreadable, which is the least useful state a file can be in.
                budget = prefer_file_bytes if path in preferred else max_file_bytes
                # Size first: decoding a 200MB blob to discover it is too large is a
                # cost with no answer attached.
                if len(raw) > budget:
                    files[path] = ""
                    continue
                try:
                    files[path] = raw.decode()
                except UnicodeDecodeError:
                    files[path] = ""
            return files

    async def remote_default_branch(
        self, url: str, *, token: str | None = None, userinfo: str | None = None
    ) -> str | None:
        """What the remote itself calls its default branch.

        Asked of git rather than of a provider API: ``ls-remote --symref`` works against
        GitHub, GitLab, Gitea, a bare path or anything else that speaks the protocol,
        needs no API scope, and cannot disagree with what a fetch would actually find.

        This has to be asked because the store deliberately forgets it. ``clone_from``
        renames the imported head to ``main`` so branch and pull-request conventions hold
        store-wide, which is a reasonable internal choice that happens to discard the one
        fact an outbound operation needs -- a repository whose remote calls it ``master``
        could not be refreshed at all, and said only "couldn't find remote ref main".
        """
        proc = await asyncio.create_subprocess_exec(
            "git",
            "ls-remote",
            "--symref",
            authed_url(url, token, userinfo),
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", **_GIT_ENV},
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise GitStoreError(
                redact_credentials(f"could not read the remote: {err.decode()[:200]}")
            )
        for line in out.decode(errors="replace").splitlines():
            if line.startswith("ref:") and line.rstrip().endswith("HEAD"):
                ref = line.split()[1]
                return ref.removeprefix("refs/heads/")
        return None

    async def fetch_from(
        self,
        repo_key: str,
        url: str,
        *,
        token: str | None = None,
        userinfo: str | None = None,
        remote_branch: str | None = None,
        local_branch: str = "main",
    ) -> FetchResult:
        """Catch ``branch`` up with its external remote, without ever losing work.

        Registration clones once and ``clone_from`` then refuses to overwrite, which is
        right -- the hosted store is where agents commit and where approved pull
        requests land, so it is not a mirror to be reset. But nothing re-read the remote
        *at all*, so a repository registered from GitHub diverged silently and
        permanently the moment anyone pushed to it elsewhere, and the knowledge graph
        went on describing whatever the code looked like on registration day.

        Fast-forward only, and never ``--force``. If ``main`` has moved on both sides
        this reports ``diverged`` and changes nothing: the store's own commits may be
        merged pull requests that exist nowhere else, and picking a winner silently is
        how that work would disappear. ``pyr/*`` work branches are never touched --
        a single-branch fetch lands in FETCH_HEAD and only ``branch`` is merged.
        """
        # The remote's name for its default branch is not the store's: `clone_from`
        # normalises the local one to `main`. Ask the remote rather than assume.
        if remote_branch is None:
            remote_branch = (
                await self.remote_default_branch(url, token=token, userinfo=userinfo)
                or local_branch
            )
        fetch_url = authed_url(url, token, userinfo)
        async with self._locks[repo_key]:
            before = (await self._git(repo_key, "rev-parse", local_branch)).strip()
            await self._git(repo_key, "fetch", "-q", fetch_url, remote_branch)
            remote_sha = (await self._git(repo_key, "rev-parse", "FETCH_HEAD")).strip()

            if remote_sha == before:
                return FetchResult("unchanged", before, before)

            base = (await self._git(repo_key, "merge-base", before, remote_sha)).strip()
            if base == remote_sha:
                # The remote is an ancestor: we are in front of it, which merge_branch
                # does routinely. Nothing to pull.
                ahead = int(
                    (
                        await self._git(repo_key, "rev-list", "--count", f"{remote_sha}..{before}")
                    ).strip()
                    or "0"
                )
                return FetchResult("ahead", before, before, ahead=ahead)
            if base != before:
                behind = int(
                    (
                        await self._git(repo_key, "rev-list", "--count", f"{base}..{remote_sha}")
                    ).strip()
                    or "0"
                )
                ahead = int(
                    (await self._git(repo_key, "rev-list", "--count", f"{base}..{before}")).strip()
                    or "0"
                )
                return FetchResult("diverged", before, before, behind=behind, ahead=ahead)

            behind = int(
                (
                    await self._git(repo_key, "rev-list", "--count", f"{before}..{remote_sha}")
                ).strip()
                or "0"
            )
            await self._git(repo_key, "checkout", "-q", local_branch)
            await self._git(repo_key, "merge", "--ff-only", "-q", remote_sha)
            after = (await self._git(repo_key, "rev-parse", local_branch)).strip()
            return FetchResult("fast_forwarded", before, after, behind=behind)

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

    async def create_branch(self, repo_key: str, branch: str, *, base: str = "main") -> str:
        """Cut ``branch`` from ``base`` if it does not exist. Returns its head sha.

        What "cutting a release-candidate branch" is, mechanically. The working branch
        is left as it was: creating a branch should not move anybody to it.
        """
        async with self._locks[repo_key]:
            existing = await self._git(repo_key, "branch", "--list", branch)
            if not existing.strip():
                await self._git(repo_key, "branch", branch, base)
            return (await self._git(repo_key, "rev-parse", branch)).strip()

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
        self,
        repo_key: str,
        branch: str,
        base: str = "main",
        max_chars: int = 40_000,
        max_listed_files: int = 2000,
    ) -> str:
        """The unified diff for base...branch, capped — reviewer-model input.

        The cap used to be a blind slice of the diff body, which answered the wrong
        question first. Asked whether a branch deleted three named things, a reviewer
        was handed forty thousand characters of the *first* file's content and never
        learned the other ninety-four existed -- and approved, saying so: "the diff is
        truncated, so no concrete defect is visible in the provided portion."

        So the file-level summary is always complete and always first. It is small (one
        line per file, where a body is thousands) and it is what most review questions
        actually turn on: what changed, and was anything touched that should not have
        been. Bodies then fill whatever budget remains, and what was dropped is stated
        rather than implied.
        """
        async with self._locks[repo_key]:
            status = await self._git(repo_key, "diff", "--name-status", f"{base}...{branch}")
            out = await self._git(repo_key, "diff", f"{base}...{branch}")

        lines = [ln for ln in status.splitlines() if ln.strip()]
        listed, hidden = lines[:max_listed_files], max(0, len(lines) - max_listed_files)
        header = (
            f"Files changed ({len(lines)}):\n"
            + "\n".join(listed)
            + (f"\n... and {hidden} more file(s)" if hidden else "")
        )
        budget = max_chars - len(header) - 200
        if budget <= 0 or len(out) <= budget:
            body = (
                out
                if budget > 0
                else "(file contents omitted: the file list alone fills the budget)"
            )
        else:
            body = (
                out[:budget] + f"\n... (file contents truncated here; {len(out) - budget} more "
                "characters of diff body not shown. The complete list of changed files "
                "is above -- judge scope from that, not from the portion below.)"
            )
        return f"{header}\n\n{body}"

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

    async def list_prs(self, repo_key: str) -> list[dict[str, Any]]:
        """Every recorded pull request, newest number first, each carrying its branch.

        The sidecar is keyed by branch and the branch is what a preview needs to name, so
        it is folded into the record rather than left as a key the caller has to zip back
        together."""
        async with self._locks[repo_key]:
            prs = self._load_prs(repo_key)
        records = [{**record, "branch": branch} for branch, record in prs.items()]
        records.sort(key=lambda r: int(r.get("number") or 0), reverse=True)
        return records

    async def next_pr_number(self, repo_key: str) -> int:
        async with self._locks[repo_key]:
            return len(self._load_prs(repo_key)) + 1

    async def reset(self, repo_key: str) -> None:
        """Drop a repo entirely (test/verification convenience)."""
        shutil.rmtree(self._repo_dir(repo_key), ignore_errors=True)
