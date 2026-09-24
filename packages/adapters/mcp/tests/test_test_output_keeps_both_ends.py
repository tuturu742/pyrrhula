"""A long test run must not lose the failures to keep the tally.

Runners print each failure as it happens and the count at the end, so keeping only the
tail drops the earliest failures first. The reviewer reported exactly this: "The build
log is cut off at the top. I can't see whether layout_snapshot_80x24,
layout_snapshot_120x30 and header_snapshot_offline_with_downloads also fail."
"""

from adapters.mcp.git_transport import _both_ends


def test_short_output_is_untouched() -> None:
    assert _both_ends("364 passed; 0 failed", 500) == "364 passed; 0 failed"


def test_the_first_failure_and_the_tally_both_survive() -> None:
    run = (
        "FAILED layout_snapshot_80x24: expected 01:00 got 23:00\n"
        + ("filler line that is not interesting\n" * 400)
        + "test result: FAILED. 364 passed; 5 failed"
    )
    kept = _both_ends(run, 600)
    assert "layout_snapshot_80x24" in kept, "the earliest failure was dropped"
    assert "364 passed; 5 failed" in kept, "the tally was dropped"
    assert "omitted" in kept, "the reader is not told anything was cut"
    assert len(kept) < len(run)


def test_dependency_chatter_does_not_eat_the_budget() -> None:
    """A build tool prints one progress line per dependency, hundreds of them, none of
    which says anything about the run. Keeping them cost the whole budget: the reviewer
    reported "the build log is cut off during crate downloads, before compilation or any
    test output", and so reached a verdict without a single test name.
    """
    from adapters.mcp.git_transport import _strip_build_noise

    run = (
        "\n".join(f"  Downloaded dep-{i} v1.0.{i}" for i in range(300))
        + "\n   Compiling loxia-tui v0.1.0\n"
        + "FAILED layout_snapshot_80x24: expected 01:00 got 23:00\n"
        + "test result: FAILED. 364 passed; 5 failed"
    )
    kept = _both_ends(_strip_build_noise(run), 400)
    assert "layout_snapshot_80x24" in kept, "the failure was crowded out by progress lines"
    assert "364 passed; 5 failed" in kept
    assert "Downloaded dep-" not in kept


def test_a_tests_own_output_is_not_mistaken_for_chatter() -> None:
    """The prefixes are anchored, so a test that prints one of those words mid-line, or
    asserts about downloads, keeps its output."""
    from adapters.mcp.git_transport import _strip_build_noise

    run = (
        "widgets::header::tests::header_snapshot_offline_with_downloads FAILED\n"
        "  assertion failed: Downloading 3 tracks shown as 'Downloaded'\n"
    )
    kept = _strip_build_noise(run)
    assert "header_snapshot_offline_with_downloads" in kept
    assert "assertion failed" in kept
