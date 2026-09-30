"""Reading a harness run out of its own output.

The fixtures below are the shapes opencode 1.18.33 actually emitted in the spike and the
first end-to-end delegation, not invented ones -- including the error event, which is what
a 401 from the proxy looks like from inside the container.
"""

from __future__ import annotations

from core.harness.events import summarise

# Verbatim shapes, trimmed. Note the stream is fenced by the work script's own markers:
# npm's install chatter precedes it and git's output follows, and neither is an event.
RUN = """npm notice New major version of npm available!
PYR_STEP=harness
{"type":"step_start","timestamp":1,"sessionID":"ses_1","part":{"type":"step-start"}}
{"type":"tool_use","timestamp":2,"sessionID":"ses_1","part":{"type":"tool","tool":"bash",\
"callID":"call_1","state":{"status":"completed","input":{"command":"ls -la && cat package.json"},\
"output":"total 8"}}}
{"type":"tool_use","timestamp":3,"sessionID":"ses_1","part":{"type":"tool","tool":"write",\
"callID":"call_2","state":{"status":"completed","input":{"filePath":"src/greet.js"},"output":""}}}
{"type":"tool_use","timestamp":4,"sessionID":"ses_1","part":{"type":"tool","tool":"write",\
"callID":"call_3","state":{"status":"completed","input":{"filePath":"test/greet.test.js"},"output":""}}}
{"type":"tool_use","timestamp":5,"sessionID":"ses_1","part":{"type":"tool","tool":"bash",\
"callID":"call_4","state":{"status":"completed","input":{"command":"npm test"},"output":"ok 1"}}}
{"type":"step_finish","timestamp":6,"sessionID":"ses_1","part":{"type":"step-finish",\
"tokens":{"total":7405,"input":181,"output":48,"reasoning":8,"cache":{"write":0,"read":7168}}}}
{"type":"step_finish","timestamp":7,"sessionID":"ses_1","part":{"reason":"tool-calls",\
"type":"step-finish","tokens":{"input":1800,"output":200,"reasoning":0,\
"cache":{"write":0,"read":0}},"cost":0}}
{"type":"text","timestamp":8,"sessionID":"ses_1","part":{"type":"text",\
"text":"Done. Both tests pass."}}
PYR_HARNESS_RC=0
git add -A -- .
PYR_STEP=test
> node --test test/
"""

FAILED = """PYR_STEP=harness
{"type":"error","timestamp":1,"sessionID":"ses_2","error":{"name":"APIError",\
"data":{"message":"Missing Authentication header","statusCode":401,"isRetryable":false}}}
PYR_HARNESS_RC=1
"""


def test_it_reads_the_steps_the_harness_took() -> None:
    run = summarise(RUN)
    assert len(run.steps) == 4
    assert run.tool_counts == {"bash": 2, "write": 2}
    assert run.steps[0].detail == "ls -la && cat package.json"
    assert run.steps[1].detail == "src/greet.js", "a write names the file, not a command"
    assert all(step.outcome == "completed" for step in run.steps)


def test_it_takes_the_last_thing_the_harness_said_as_its_conclusion() -> None:
    assert summarise(RUN).said == "Done. Both tests pass."


def test_it_totals_the_tokens_the_harness_reported() -> None:
    """Advisory, not authoritative -- the proxy's usage rows are the record. This is for
    the persona's own sense of what its work cost.

    The two fixture steps carry the two shapes opencode emitted in a single real run: a
    step ending on `stop` has `total`, one ending on `tool-calls` has only the components.
    Reading `total` alone counted every tool-calling run -- which is every coding run --
    as zero.
    """
    assert summarise(RUN).tokens == 7405 + 2000


def test_the_summary_is_bounded_and_reads_as_the_persona_speaking() -> None:
    """It becomes the persona's own transcript turn, replayed to it as something it said,
    and it lands in the same history budget as the conversation."""
    summary = summarise(RUN).summary()
    assert "4 steps" in summary
    assert "bash ×2" in summary and "write ×2" in summary
    assert "9,405 tokens" in summary
    assert "Done. Both tests pass." in summary
    assert len(summary) <= 601, "a verbose summary evicts the conversation it informs"


def test_noise_outside_the_harness_step_is_not_an_event() -> None:
    """npm's chatter comes before and git's output after; neither is the harness talking."""
    run = summarise(RUN)
    assert not any("npm notice" in step.detail for step in run.steps)
    assert "node --test" not in run.said


def test_a_failed_run_carries_what_went_wrong() -> None:
    """A 401 from the proxy, seen from inside the container. Without this the transcript
    would show a delegation that did nothing and said nothing about why."""
    run = summarise(FAILED)
    assert run.errors == ["Missing Authentication header"]
    assert "Missing Authentication header" in run.summary()


def test_output_with_no_harness_step_summarises_to_nothing() -> None:
    """The one-shot codegen path. It must produce no note at all, rather than an empty
    one."""
    assert summarise("PYR_STEP=write\nPYR_STEP=test\nok 1\n").summary() == ""


def test_rubbish_is_skipped_rather_than_raised_on() -> None:
    """A delegation that did the work and then failed while describing it would be a poor
    trade."""
    run = summarise('PYR_STEP=harness\nnot json\n{"type":"nonsense"}\n{broken\n')
    assert run.steps == [] and run.said == ""
