"""A reviewer that cannot see the build cannot refuse a red one.

The review prompt carried the work item and the diff and nothing else, so every verdict
it ever reached was a reading of the diff. That is fine for finding defects in code and
useless for the one rule the delivery samples exist to demonstrate -- and it showed:
on a branch whose suite was red, the lead approved a scope-correct one-file change with
"the change plausibly implements verdict (a) ... and has no concrete defect". It was
right about the diff. Nothing in front of it said five tests had failed.

Approving a red build is the review error that cannot be caught downstream, because the
merge order is planned from approvals.
"""

from __future__ import annotations

from worker.review import _REVIEW_SYSTEM, _build_line


def test_a_failed_build_is_put_in_front_of_the_reviewer() -> None:
    line = _build_line(
        {
            "ci_status": "failed",
            "summary": "Implement: x on pyr/a-1 (2 commit(s)); #52 open; tests failed\n"
            "test result: FAILED. 364 passed; 5 failed",
        }
    )
    assert "FAILED" in line
    # The failing-test tail travels with it, so the reviewer can name them.
    assert "364 passed; 5 failed" in line


def test_a_red_suite_is_not_by_itself_declared_the_verdict() -> None:
    """This line states a fact; the prompt weighs it. It used to say "This is
    request_changes", which is correct when the item's own target is what failed and
    wrong for everything else -- against a trunk that is already red (loxia ships five
    failing fixtures on purpose) it made every work item unapprovable forever, including
    one that genuinely fixed its own test. That is the approve half of the review loop
    silently switched off."""
    line = _build_line({"ci_status": "failed", "summary": "tests failed"})
    assert "request_changes" not in line
    assert "own target" in line


def test_a_passing_build_says_so_plainly() -> None:
    assert _build_line({"ci_status": "passed"}) == "Build: tests PASSED on this branch."


def test_an_absent_result_is_not_silently_read_as_success() -> None:
    """A branch with no recorded result must not look like a green one: the reviewer is
    told to say so rather than assume either way."""
    line = _build_line({})
    assert "no test result" in line
    assert "PASSED" not in line


def test_the_system_prompt_makes_a_red_build_decisive() -> None:
    """Stating the result is not enough on its own -- the rule has to be in the
    instructions, or a model weighing 'plausibly implements the work item' against it
    can still land on approve."""
    assert "request_changes" in _REVIEW_SYSTEM
    assert "red" in _REVIEW_SYSTEM.lower()


def test_the_reviewer_prefers_what_the_run_actually_printed() -> None:
    """`summary` is a label; `test_output` is the run. A record carrying both should
    show the reviewer the second one."""
    line = _build_line(
        {
            "ci_status": "failed",
            "summary": "Implement: x on pyr/a-1; #52 open; tests failed",
            "test_output": "assertion failed: snapshot mismatch\n-[unbound]\n+[H]",
        }
    )
    assert "snapshot mismatch" in line


def test_an_older_record_without_test_output_still_shows_its_summary() -> None:
    """Records written before `test_output` existed carry only `summary`, and a reviewer
    shown an empty string would read it as "no detail available"."""
    line = _build_line({"ci_status": "failed", "summary": "364 passed; 5 failed"})
    assert "364 passed; 5 failed" in line


def test_the_reviewer_is_shown_the_end_of_a_long_record() -> None:
    """Same rule as the rework brief: failures and the tally are at the end."""
    detail = ("compile chatter\n" * 4000) + "snapshot assertion for 'layout_snapshot_80x24' failed"
    line = _build_line({"ci_status": "failed", "test_output": detail})
    assert "layout_snapshot_80x24" in line
