"""GitStore broken-ref sweep: a zero-length loose ref (a process killed mid-write)
must be removed on ensure_repo, never left to poison every later clone/fetch."""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.mcp.git_store import GitStore, GitStoreError


async def test_ensure_repo_sweeps_zero_length_refs(tmp_path: Path) -> None:
    store = GitStore(str(tmp_path))
    await store.ensure_repo("proj")

    git_dir = tmp_path / "proj" / "repo" / ".git"
    refs = git_dir / "refs" / "heads" / "pyr"
    refs.mkdir(parents=True, exist_ok=True)
    broken = refs / "abcd1234-1"
    broken.touch()  # the crash artifact: an empty ref file, advertised as all-zeros

    await store.ensure_repo("proj")
    assert not broken.exists()
    # Healthy refs survive the sweep.
    assert (git_dir / "refs" / "heads" / "main").exists() or (git_dir / "packed-refs").exists()


async def test_merge_branch_lands_work_on_main(tmp_path) -> None:
    """A work branch must be able to land, or the next work item starts from a base that
    is missing its predecessor's code and fails against it."""
    store = GitStore(str(tmp_path))
    await store.ensure_repo("proj", seed_files={"a.txt": "one\n"})
    await store.commit_on_branch("proj", "pyr/w-0", {"b.txt": "two\n"}, "add b")

    tree_before = await store.read_tree("proj")
    assert "b.txt" not in tree_before, "branch content must not be on main before merging"

    sha = await store.merge_branch("proj", "pyr/w-0")
    assert sha
    tree_after = await store.read_tree("proj")
    assert tree_after["b.txt"] == "two\n"
    assert tree_after["a.txt"] == "one\n"


async def test_second_branch_sees_the_first_after_merge(tmp_path) -> None:
    store = GitStore(str(tmp_path))
    await store.ensure_repo("proj", seed_files={"a.txt": "one\n"})
    await store.commit_on_branch("proj", "pyr/w-0", {"b.txt": "two\n"}, "add b")
    await store.merge_branch("proj", "pyr/w-0")
    # Cut after the merge: the new branch inherits b.txt.
    await store.commit_on_branch("proj", "pyr/w-1", {"c.txt": "three\n"}, "add c")
    await store.merge_branch("proj", "pyr/w-1")

    tree = await store.read_tree("proj")
    assert {"a.txt", "b.txt", "c.txt"} <= set(tree)


async def test_merge_conflict_aborts_rather_than_committing_markers(tmp_path) -> None:
    store = GitStore(str(tmp_path))
    await store.ensure_repo("proj", seed_files={"a.txt": "base\n"})
    await store.commit_on_branch("proj", "pyr/x-0", {"a.txt": "from x\n"}, "x")
    await store.commit_on_branch("proj", "pyr/y-0", {"a.txt": "from y\n"}, "y")
    await store.merge_branch("proj", "pyr/x-0")

    with pytest.raises(GitStoreError):
        await store.merge_branch("proj", "pyr/y-0")
    tree = await store.read_tree("proj")
    assert "<<<<<<<" not in tree["a.txt"], "conflict markers were committed"
    assert tree["a.txt"] == "from x\n"


def test_a_failed_git_command_never_echoes_the_token() -> None:
    """Regression (credential disclosure): `authed_url` puts a live token in a URL's
    userinfo because that is the only way to hand git a credential without persisting one.
    git redacts credentials from its own stderr, which made this look safe -- but the error
    raised here composes the argv *we* passed, and the token is in the argv. A push to a
    repository the token could not write logged the whole PAT in plaintext, at warning
    level, where it reached the worker's stdout and anything aggregating it."""
    from adapters.mcp.git_store import authed_url, redact_credentials

    token = "github_pat_11EXAMPLEONLY_notarealtoken"  # noqa: S105
    url = authed_url("https://github.com/acme/widget", token)
    assert token in url, "precondition: the token really is in the URL git is handed"

    message = redact_credentials(f"git push -q {url} main:main failed: remote: Permission denied")

    assert token not in message
    assert "x-access-token" not in message
    assert "https://***@github.com/acme/widget" in message
    assert "Permission denied" in message, "the diagnosis must survive the redaction"


def test_redaction_leaves_ordinary_urls_alone() -> None:
    """Redaction must not mangle the messages it has no reason to touch."""
    from adapters.mcp.git_store import redact_credentials

    message = "clone failed: repository 'https://github.com/acme/widget' not found"
    assert redact_credentials(message) == message


async def test_read_tree_survives_a_binary_file(tmp_path: Path) -> None:
    """A repository with one non-UTF-8 file must still be readable.

    `git show` on a binary blob *succeeds*, so the failure arrived as a
    `UnicodeDecodeError` from the decode rather than the `GitStoreError` read_tree
    caught -- and escaped to kill the caller's whole job. Observed live: a workspace
    with three repositories failed `analyze_workspace_repos` outright with
    "'utf-8' codec can't decode byte 0xc1", and the UI showed the analysis starting and
    then simply nothing.
    """
    store = GitStore(str(tmp_path))
    await store.ensure_repo(
        "proj",
        seed_files={
            "README.md": "# proj\n",
            # A real PNG header: 0x89 is not valid UTF-8 in this position.
            "logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xc1\xc2\xc3",
            "src/main.py": "print('hi')\n",
        },
    )

    tree = await store.read_tree("proj")

    # The binary file is listed -- its *path* still tells a model something -- with no
    # content, exactly as the docstring has always promised.
    assert tree["logo.png"] == ""
    # ...and, the point of the test, the text files around it came through.
    assert tree["README.md"] == "# proj\n"
    assert tree["src/main.py"] == "print('hi')\n"


async def test_read_tree_skips_oversized_files_by_byte_length(tmp_path: Path) -> None:
    """Size is measured on the bytes, before any decode: discovering a 200MB blob is too
    large by first decoding it is a cost with no answer attached."""
    store = GitStore(str(tmp_path))
    await store.ensure_repo("proj", seed_files={"small.txt": "ok\n", "big.txt": "x" * 5_000})

    tree = await store.read_tree("proj", max_file_bytes=100)

    assert tree["small.txt"] == "ok\n"
    assert tree["big.txt"] == ""


async def _remote_with_commit(tmp_path: Path, name: str, content: str) -> str:
    """A real git repository on disk, usable as a `file://` remote."""
    remote = GitStore(str(tmp_path / "remotes"))
    await remote.ensure_repo(name, seed_files={"README.md": content})
    return f"file://{tmp_path / 'remotes' / name / 'repo'}"


async def test_fetch_from_fast_forwards_when_only_the_remote_moved(tmp_path: Path) -> None:
    """The case the whole feature exists for: somebody pushed to GitHub, and the store
    had no way to hear about it."""
    url = await _remote_with_commit(tmp_path, "origin", "# v1\n")
    remote = GitStore(str(tmp_path / "remotes"))
    store = GitStore(str(tmp_path / "store"))
    await store.clone_from("proj", url)

    await remote.commit_on_branch("origin", "main", {"README.md": "# v2\n"}, "second")

    result = await store.fetch_from("proj", url)

    assert result.status == "fast_forwarded", result
    assert result.behind == 1
    assert result.after_sha != result.before_sha
    assert (await store.read_tree("proj"))["README.md"] == "# v2\n"


async def test_fetch_from_is_a_no_op_when_nothing_moved(tmp_path: Path) -> None:
    url = await _remote_with_commit(tmp_path, "origin", "# v1\n")
    store = GitStore(str(tmp_path / "store"))
    await store.clone_from("proj", url)

    result = await store.fetch_from("proj", url)

    assert result.status == "unchanged"
    assert result.before_sha == result.after_sha


async def test_fetch_from_reports_ahead_rather_than_rewinding_merged_work(
    tmp_path: Path,
) -> None:
    """An approved pull request merges into `main` server-side, so the store leading the
    remote is ordinary, not an error. It must not be "fixed" by pulling."""
    url = await _remote_with_commit(tmp_path, "origin", "# v1\n")
    store = GitStore(str(tmp_path / "store"))
    await store.clone_from("proj", url)
    await store.commit_on_branch("proj", "main", {"AGENT.md": "landed by an agent\n"}, "merged PR")
    before = await store.head_sha("proj")

    result = await store.fetch_from("proj", url)

    assert result.status == "ahead", result
    assert result.ahead == 1
    assert await store.head_sha("proj") == before
    # The agent's commit is still there -- the point of the test.
    assert "AGENT.md" in await store.read_tree("proj")


async def test_fetch_from_refuses_to_pick_a_winner_when_both_moved(tmp_path: Path) -> None:
    """Divergence is the dangerous case: the store's commits may be merged pull requests
    that exist nowhere else, so a fast-forward is impossible and a reset would delete
    them. Report it and change nothing."""
    url = await _remote_with_commit(tmp_path, "origin", "# v1\n")
    remote = GitStore(str(tmp_path / "remotes"))
    store = GitStore(str(tmp_path / "store"))
    await store.clone_from("proj", url)

    await remote.commit_on_branch("origin", "main", {"REMOTE.md": "theirs\n"}, "remote work")
    await store.commit_on_branch("proj", "main", {"OURS.md": "ours\n"}, "server-side work")
    before = await store.head_sha("proj")

    result = await store.fetch_from("proj", url)

    assert result.status == "diverged", result
    assert result.ahead == 1 and result.behind == 1
    assert await store.head_sha("proj") == before
    tree = await store.read_tree("proj")
    assert "OURS.md" in tree and "REMOTE.md" not in tree


async def test_fetch_from_leaves_work_branches_alone(tmp_path: Path) -> None:
    """A single-branch fetch and a main-only merge: `pyr/*` must survive untouched."""
    url = await _remote_with_commit(tmp_path, "origin", "# v1\n")
    remote = GitStore(str(tmp_path / "remotes"))
    store = GitStore(str(tmp_path / "store"))
    await store.clone_from("proj", url)
    await store.commit_on_branch("proj", "pyr/work-1", {"WIP.md": "in progress\n"}, "wip")
    wip_sha = await store.head_sha("proj", "pyr/work-1")

    await remote.commit_on_branch("origin", "main", {"README.md": "# v2\n"}, "second")
    assert (await store.fetch_from("proj", url)).status == "fast_forwarded"

    assert await store.head_sha("proj", "pyr/work-1") == wip_sha


async def test_clone_from_does_not_leave_the_token_on_disk(tmp_path: Path) -> None:
    """The credential must not survive the clone that used it.

    `git clone` writes the URL it was handed into `.git/config` as `origin`, so a token
    passed for a private remote was persisted in plaintext on the volume -- readable by
    anything that could reach it, surviving container recreates, and riding along in
    backups. Found live on a deployment where all five repositories held a writable
    GitHub token.
    """
    url = await _remote_with_commit(tmp_path, "origin", "# v1\n")
    store = GitStore(str(tmp_path / "store"))

    await store.clone_from("proj", url, token="ghp_a_real_looking_secret")

    config = (tmp_path / "store" / "proj" / "repo" / ".git" / "config").read_text()
    assert "ghp_a_real_looking_secret" not in config
    assert "@" not in config.split("[remote")[1] if "[remote" in config else True
    # The repository is still usable and still knows where it came from.
    assert (await store.read_tree("proj"))["README.md"] == "# v1\n"


async def test_clone_from_keeps_working_without_a_token(tmp_path: Path) -> None:
    url = await _remote_with_commit(tmp_path, "origin", "# v1\n")
    store = GitStore(str(tmp_path / "store"))

    await store.clone_from("proj", url)

    assert (await store.read_tree("proj"))["README.md"] == "# v1\n"


async def test_fetch_from_follows_a_remote_that_calls_it_master(tmp_path: Path) -> None:
    """The store renames the imported head to `main`; the remote keeps its own name.

    Observed live: two of three registered repositories used `master` on GitHub, and
    refresh failed on both with "couldn't find remote ref main" -- while the third,
    which used `main`, worked. The remote's name has to be asked for, not assumed.
    """
    remote = GitStore(str(tmp_path / "remotes"))
    await remote.ensure_repo("origin", seed_files={"README.md": "# v1\n"})
    # Make the remote's default branch `master`, as the older GitHub default did.
    await remote._git("origin", "branch", "-m", "main", "master")
    url = f"file://{tmp_path / 'remotes' / 'origin' / 'repo'}"

    store = GitStore(str(tmp_path / "store"))
    imported = await store.clone_from("proj", url)
    # The imported branch keeps the remote's name, and clone_from reports it so the
    # caller can record it. It used to be renamed to `main`, which is precisely how the
    # remote's real name got lost.
    assert imported == "master"
    assert await store.head_sha("proj", "master")

    # Driven with raw git: commit_on_branch returns to a hardcoded `main`, which this
    # deliberately-master remote does not have.
    (tmp_path / "remotes" / "origin" / "repo" / "README.md").write_text("# v2\n")
    await remote._git("origin", "add", "-A")
    await remote._git("origin", "commit", "-q", "-m", "second")

    result = await store.fetch_from("proj", url, local_branch="master")

    assert result.status == "fast_forwarded", result
    assert (await store.read_tree("proj", ref="master"))["README.md"] == "# v2\n"


async def test_clone_from_reports_the_branch_it_imported(tmp_path: Path) -> None:
    """The caller stores this on the repository row; nothing else can recover it later
    without asking the remote again."""
    url = await _remote_with_commit(tmp_path, "origin", "# v1\n")
    store = GitStore(str(tmp_path / "store"))

    assert await store.clone_from("proj", url) == "main"


async def test_remote_default_branch_reads_it_from_the_remote(tmp_path: Path) -> None:
    remote = GitStore(str(tmp_path / "remotes"))
    await remote.ensure_repo("origin", seed_files={"README.md": "x\n"})
    await remote._git("origin", "branch", "-m", "main", "trunk")
    url = f"file://{tmp_path / 'remotes' / 'origin' / 'repo'}"

    store = GitStore(str(tmp_path / "store"))
    assert await store.remote_default_branch(url) == "trunk"
