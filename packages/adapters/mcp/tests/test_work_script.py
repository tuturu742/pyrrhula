"""The work script's git remote: per-engine git_http_base override vs the deployment
default (remote engines' environments may not resolve the default hostname)."""

from __future__ import annotations

from pathlib import Path

from adapters.mcp.git_store import GitStore
from adapters.mcp.git_transport import GitMcpTransport


def _script(tmp_path: Path, env_cfg: dict) -> str:
    transport = GitMcpTransport(GitStore(str(tmp_path)))
    return transport._build_work_script(  # noqa: SLF001 -- the seam under test
        "proj", "pyr/abcd1234-0", {"a.txt": "hi"}, "msg", env_cfg
    )


def test_engine_git_http_base_overrides_default(tmp_path: Path) -> None:
    script = _script(tmp_path, {"git_http_base": "http://192.0.2.7:8000"})
    assert "http://job:" in script
    assert "@192.0.2.7:8000/git/proj" in script


def test_default_base_without_override(tmp_path: Path) -> None:
    script = _script(tmp_path, {})
    assert "@pyrrhula_api_1:8000/git/proj" in script


def test_build_and_artifact_stage_when_configured(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        {
            "test_cmd": "true",
            "build_cmd": "make export",
            "artifact_name": "game-web.zip",
        },
    )
    assert "PYR_STEP=build" in script
    assert "( make export )" in script
    assert "PYR_STEP=artifact" in script
    assert "/artifact" in script and "name=game-web.zip" in script
    assert "wget -q -O - --method=POST" in script  # curl-less CI images
    # Build output never enters history. It is deleted after upload rather than excluded
    # by pathspec -- see test_gitignored_artifact_is_removed_before_commit.
    assert "rm -f game-web.zip" in script
    # build gated on the test verdict, upload gated on the build verdict + file
    assert 'if [ "$PYR_TEST_RC" = "0" ] || [ "$PYR_TEST_RC" = "-1" ]' in script
    assert "[ -f game-web.zip ]" in script


def test_no_build_stage_without_artifact_name(tmp_path: Path) -> None:
    script = _script(tmp_path, {"test_cmd": "true", "build_cmd": "make export"})
    assert "PYR_STEP=build" not in script


def test_gitignored_artifact_is_removed_before_commit(tmp_path: Path) -> None:
    """Regression: a build artifact that is also in .gitignore made `git add` exit
    non-zero, so the branch was pushed still pointing at its base commit -- the agent's
    work passed CI, uploaded its artifact, and was then silently thrown away."""
    transport = GitMcpTransport(GitStore(str(tmp_path)))
    script = transport._build_work_script(  # noqa: SLF001 -- the seam under test
        "proj",
        "pyr/abcd1234-0",
        {"a.txt": "hi"},
        "msg",
        {"build_cmd": "make web", "artifact_name": "game-web.tar.gz"},
    )
    push = script[script.index("PYR_STEP=push") :]
    assert "rm -f game-web.tar.gz" in push, "artifact not deleted before commit"
    assert ":(exclude)" not in push, "pathspec exclude reintroduced; it breaks on ignored files"
    assert "git add" not in push, "staging moved back after the build; build output would commit"


def test_no_artifact_no_removal(tmp_path: Path) -> None:
    transport = GitMcpTransport(GitStore(str(tmp_path)))
    script = transport._build_work_script(  # noqa: SLF001
        "proj",
        "pyr/abcd1234-0",
        {"a.txt": "hi"},
        "msg",
        {},
    )
    assert "rm -f" not in script[script.index("PYR_STEP=push") :]


def test_work_script_removes_deleted_paths_and_uses_the_repos_base_branch() -> None:
    """A signature that accepts an argument and a call site that does not pass it look
    identical from the outside: the script builder grew `deletes` and `base_branch`, the
    caller kept its old positional call, and both silently took their defaults. Codegen
    produced 95 deletions, the transport carried them, and the commit was empty -- with
    the branch still pointing at its base and nothing anywhere saying why.
    """
    from adapters.mcp.git_transport import GitMcpTransport

    transport = GitMcpTransport.__new__(GitMcpTransport)
    script = transport._build_work_script(
        "proj",
        "pyr/w-1",
        {"kept.py": "x\n"},
        "msg",
        {"image": "python:3.12", "setup_cmds": [], "test_cmd": ""},
        base_branch="master",
        deletes=frozenset({"tasks", "doc.md"}),
    )

    assert "rm -rf tasks" in script
    assert "rm -rf doc.md" in script
    # The fallback base is the repository's branch, not a hardcoded `main`.
    assert "origin/master" in script
    assert "origin/main" not in script


def test_the_agents_work_is_staged_before_the_tests_run(tmp_path: Path) -> None:
    """Regression, found on a live delegation batch: the index was built at push time
    with `git add -A`, so whatever the TEST step wrote landed in the commit.

    The repository under work had five failing `insta` snapshot assertions on its base
    branch. insta writes a `.snap.new` file per failure, so every agent committed five
    artefacts that had nothing to do with its task. The reviewer spotted them and asked
    for their removal, the rework agent removed them, the test step wrote them again and
    `git add -A` restaged them: two review rounds, the same complaint twice, and no way
    for the loop to converge on work that was otherwise fine.
    """
    transport = GitMcpTransport(GitStore(str(tmp_path)))
    script = transport._build_work_script(  # noqa: SLF001 -- the seam under test
        "proj",
        "pyr/abcd1234-0",
        {"a.txt": "hi"},
        "msg",
        {"test_cmd": "cargo test"},
    )
    assert script.index("git add -A -- .") < script.index("PYR_STEP=test")
    assert "git add" not in script[script.index("PYR_STEP=push") :]


def test_deletions_are_staged_too(tmp_path: Path) -> None:
    """Staging early must still capture removals -- `git add -A` stages a delete, but
    only for paths already gone when it runs."""
    transport = GitMcpTransport(GitStore(str(tmp_path)))
    script = transport._build_work_script(  # noqa: SLF001
        "proj",
        "pyr/abcd1234-0",
        {"a.txt": "hi"},
        "msg",
        {"test_cmd": "cargo test"},
        deletes=frozenset({"docs/plan.md"}),
    )
    assert script.index("rm -rf docs/plan.md") < script.index("git add -A -- .")


def test_a_store_local_ref_is_not_mistaken_for_a_host_number() -> None:
    """A pull request that failed to open gets the store's own ref (``PR-28``). The sync
    parses refs to ask the host about them, and ``PR-28`` must not become ``28`` -- there
    is very likely a real pull request #28, belonging to someone else's work."""
    from worker.pr_sync import _pr_number

    assert _pr_number({"pr_ref": "PR-28"}) is None
    assert _pr_number({"pr_ref": "#28"}) == 28


def test_a_failed_remote_open_says_so_instead_of_reading_as_success(tmp_path: Path) -> None:
    """Degrading to the store ref is right; reporting it as "Opened PR-28" is not. That
    note is indistinguishable from a real open, and a reviewer sent to a pull request
    that does not exist cannot tell which happened."""
    from adapters.mcp.git_transport import _wanted_a_remote_pr

    assert _wanted_a_remote_pr({"remote": {"url": "https://h/o/r", "credential_ref": "c"}})
    # A store-only repo never expected one, so it must not be reported as a failure.
    assert not _wanted_a_remote_pr({})
    assert not _wanted_a_remote_pr({"remote": {"url": "https://h/o/r"}})
