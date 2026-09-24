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
