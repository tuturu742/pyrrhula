"""The remote-tools synthetic phase must be a VALID PhaseSpec (learned the hard way:
an invalid literal here crashes every turn in a workspace with a registered remote
server), and the remote/preset routing predicate must never surface preset keys."""

from core.process.session_remote_tools import _is_remote, _remote_phase


def test_remote_phase_is_a_valid_phase_spec() -> None:
    phase = _remote_phase(["run_tests", "generate_image"])
    assert phase.tools == ["run_tests", "generate_image"]
    assert phase.visibility.entity_fields == []
    assert phase.visibility.secrets == "none"


def test_is_remote_excludes_presets_and_non_http() -> None:
    assert _is_remote("engine", "http://godot-mcp:8090/mcp")
    assert _is_remote("assets", "https://comfy.internal/mcp")
    assert not _is_remote("web_search", "https://searx.internal")
    # Not cosmetic: this path gates on workspace registration alone, so anything it
    # surfaces reaches personas whose own web_search flag is off.
    assert not _is_remote("web_fetch", "https://fetch.local/")
    assert not _is_remote("resolution", "pyrrhula://resolution/x")
    assert not _is_remote("git", "http://mcp-git:8080")
    assert not _is_remote("git-myrepo", "http://mcp-git:8080")
    assert not _is_remote("engine", "pyrrhula://something")
