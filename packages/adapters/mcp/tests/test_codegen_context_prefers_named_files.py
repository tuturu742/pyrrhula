"""The content budget goes to the files the task names.

``read_tree`` hands the model a bounded number of files with bodies and the rest as bare
paths. The order was alphabetical, which is not a relevance ranking: asked to update five
snapshot fixtures in a Rust workspace, the agent received the first forty paths and
answered -- correctly -- that the files it had been asked about "were given as" empty. It
then fell back to a placeholder, and a reviewer rejected a pull request whose real fault
was the context it had been handed.

The task's own words are the best signal available without a second model call.
"""

from __future__ import annotations

from adapters.mcp.git_transport import _named_paths


def test_paths_the_task_names_are_extracted_longest_first() -> None:
    work_item = {
        "title": "make the five failing snapshot tests pass",
        "description": (
            "render::tests::layout_snapshot_80x24 fails; see "
            "crates/loxia-tui/src/render.rs and crates/loxia-tui/src/widgets/header.rs"
        ),
    }
    got = _named_paths(work_item, "also views/now_playing.rs")

    assert got[0] == "crates/loxia-tui/src/widgets/header.rs"
    assert "crates/loxia-tui/src/render.rs" in got
    assert "views/now_playing.rs" in got


def test_a_bare_extension_is_not_a_filter() -> None:
    """`*.snap` or `.rs` matches half the tree and would spend the whole budget on
    noise; a token needs a name in front of the dot to narrow anything."""
    got = _named_paths({"title": "update the .snap files and any .rs that needs it"}, "")
    assert ".snap" not in got
    assert ".rs" not in got


def test_nothing_named_means_no_preference() -> None:
    """A task that names no file leaves the ordering alone rather than inventing one."""
    assert _named_paths({"title": "tidy up the queue module"}, "") == []


def test_the_preference_list_is_bounded() -> None:
    """A brief that quotes a whole tree must not turn the preference into the tree."""
    many = " ".join(f"pkg/mod{i}/file{i}.rs" for i in range(200))
    assert len(_named_paths({"title": many}, "")) <= 40
