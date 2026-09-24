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
