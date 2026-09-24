"""The rework agent cannot run the suite. If nobody shows it the failure, it guesses.

Observed across the whole loxia sample: five work items, each asked to fix a snapshot
fixture, each producing a hand-written fixture that still did not match. The agent was
not being careless -- it emits file contents and the environment runs the tests, so the
bytes the renderer actually produces were never in front of it. The reviewer's prose
said a test was red; nothing said how.
"""

from worker.delegation import _with_test_output


def test_the_failure_output_reaches_the_agent() -> None:
    brief = _with_test_output(
        "Please address review feedback.",
        {
            "ci_status": "failed",
            "summary": "tests failed",
            "test_output": "snapshot assertion failed\n-[unbound]\n+[H]\n364 passed; 5 failed",
        },
    )
    assert "Please address review feedback." in brief
    assert "-[unbound]" in brief, "the agent was not shown what the run printed"
    assert "cannot run the suite" in brief


def test_an_older_record_falls_back_to_its_summary() -> None:
    """Records written before `test_output` existed carry only `summary`. Falling back
    to nothing would hand the agent a brief that says a run failed and shows none of it."""
    brief = _with_test_output("fix it", {"ci_status": "failed", "summary": "364 passed; 5 failed"})
    assert "364 passed; 5 failed" in brief


def test_a_branch_nobody_tested_is_not_reported_as_green() -> None:
    """An agent that believes the suite passed will 'fix' whatever it likes."""
    brief = _with_test_output("fix it", {"ci_status": "pending"})
    assert "no test result was recorded" in brief


def test_a_green_branch_says_so() -> None:
    brief = _with_test_output("fix it", {"ci_status": "passed"})
    assert "PASSED" in brief


def test_a_failed_run_with_no_captured_output_says_that_rather_than_nothing() -> None:
    brief = _with_test_output("fix it", {"ci_status": "failed"})
    assert "no output was captured" in brief


def test_a_long_record_is_trimmed_from_the_front_not_the_back() -> None:
    """The recorded output is already curated at the source; this is a second trim of it,
    and it must favour the same end. Trimming from the back handed the agent 32,000
    characters naming not one failing test -- a run prints setup first and its failures
    and tally last, so the front is the part that says least.

    Asserts on content rather than length, which is what the earlier tests missed: a
    brief of the right size that names nothing is the exact failure this had.
    """
    detail = ("setup chatter line\n" * 4000) + (
        "snapshot assertion for 'layout_snapshot_200x50' failed\n"
        "test result: FAILED. 365 passed; 4 failed"
    )
    brief = _with_test_output("fix it", {"ci_status": "failed", "test_output": detail})
    assert "layout_snapshot_200x50" in brief, "the failing test was trimmed away"
    assert "365 passed; 4 failed" in brief
