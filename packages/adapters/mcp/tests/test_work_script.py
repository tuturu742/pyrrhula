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
    assert push.index("rm -f") < push.index("git add"), "must be removed before staging"


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
