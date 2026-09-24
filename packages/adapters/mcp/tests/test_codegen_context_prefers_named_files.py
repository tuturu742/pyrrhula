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


def test_an_identifier_names_the_file_generated_for_it() -> None:
    """Generated files are named after the thing they belong to, not after the source
    that produces them. A task citing `render::tests::layout_snapshot_80x24` has named
    `..._render__tests__layout_snapshot_80x24.snap` without writing a path -- which is
    how an agent asked to update five fixtures was handed none of them, said so, and
    fell back to a placeholder."""
    work_item = {
        "title": "make the five failing snapshot tests pass",
        "description": (
            "render::tests::layout_snapshot_80x24, "
            "views::now_playing::tests::now_playing_snapshot_history and "
            "widgets::header::tests::header_snapshot_offline_with_downloads"
        ),
    }
    got = _named_paths(work_item, "")

    assert "layout_snapshot_80x24" in got
    assert "now_playing_snapshot_history" in got
    assert "header_snapshot_offline_with_downloads" in got


def test_paths_still_come_before_identifiers() -> None:
    """A path is a better filter than an identifier, and the budget is spent in order."""
    got = _named_paths(
        {"title": "fix crates/x/src/a.rs", "description": "test some_long_identifier_here"},
        "",
    )
    assert got.index("crates/x/src/a.rs") < got.index("some_long_identifier_here")


def test_a_short_or_two_part_identifier_is_not_a_filter() -> None:
    """`max_files` or `hit_points` would match half a tree; the budget is too small to
    spend on a guess that loose."""
    got = _named_paths({"title": "adjust max_files and the hit_points field"}, "")
    assert got == []


def test_the_payload_shape_the_delegation_actually_sends() -> None:
    """Built from ``packages/worker/delegation.py``: {id, key, name, fields, states},
    with the title and description *inside* ``fields``.

    The first version of this extractor read a fixed list of top-level keys -- title,
    description, notes -- none of which exist at that level. It found nothing, every
    task got the alphabetical default, and the agent said so in prose: "the actual
    contents ... were not available in the provided repository context (both shown empty
    above)". The unit test beside it passed throughout, because it was fed the shape I
    had assumed rather than the one the caller sends.
    """
    work_item = {
        "id": "b4b1…",
        "key": "wi-7",
        "name": "Fix failing snapshot test render::tests::layout_snapshot_80x24",
        "fields": {
            "title": (
                "Fix failing snapshot test render::tests::layout_snapshot_80x24 "
                "(crates/loxia-tui/src/snapshots/"
                "loxia_tui__render__tests__layout_snapshot_80x24.snap)"
            ),
            "description": "the code that renders it is crates/loxia-tui/src/render.rs",
            "labels": [],
            "story_points": 2,
        },
        "states": {"lifecycle": "ready"},
    }
    got = _named_paths(work_item, "")

    assert (
        "crates/loxia-tui/src/snapshots/loxia_tui__render__tests__layout_snapshot_80x24.snap" in got
    )
    assert "crates/loxia-tui/src/render.rs" in got
    assert "layout_snapshot_80x24" in got


def test_a_non_string_field_does_not_break_the_walk() -> None:
    """``fields`` carries integers and lists beside its prose."""
    assert _named_paths({"fields": {"points": 3, "labels": ["a"], "t": "x/y.rs"}}, "") == ["x/y.rs"]
