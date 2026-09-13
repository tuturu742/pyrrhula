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
