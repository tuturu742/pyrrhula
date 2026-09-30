"""What a harness did, read back out of its own event stream.

A harness runs unattended for minutes and then stops. Without this, the only trace is a
branch and a test result -- the persona whose connection paid for the work could not say
what it had done, and a human watching the transcript would see a gap and then a pull
request.

The stream is already there: opencode's ``--format json`` writes newline-delimited events
to stdout, and ``ExecEnvProvider.run_script`` hands back the whole script's output. So
there is no callback endpoint here and no agent talking to us mid-run -- the container
stays a container. When mid-run steering is wanted, *that* is what needs a channel; this
does not.

Two audiences, two shapes, because one cannot serve both. A **bounded** summary goes into
the transcript as the persona's own words, and it must stay small: it lands in the same
``history_char_budget`` the conversation uses, so a chatty harness would evict the
discussion it is meant to inform. The full per-step detail is kept for the
``container_activity`` tool to answer on demand.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# The step marker the work script emits before the harness runs, and the fence after it.
# Parsing between them keeps npm's install chatter and git's output from being mistaken
# for harness events.
_START = "PYR_STEP=harness"
_RC_MARKER = "PYR_HARNESS_RC="
_END_MARKERS = (_RC_MARKER, "PYR_STEP=")

# How much of a silent harness's own output to keep as the reason it said nothing. A
# crash before the first JSON line is plain text on stdout, and a couple of lines of it
# is the difference between "the harness did nothing" and knowing why.
_NOISE_CHARS = 300

# One line of a command, in the summary. Enough to recognise `npm test` or a `sed` that
# went wrong; not enough to paste a whole heredoc into someone's transcript.
_COMMAND_CHARS = 120
# The summary's hard ceiling. A few hundred characters is a paragraph -- deliberately
# smaller than one transcript message, because it competes with the conversation.
_SUMMARY_CHARS = 600


@dataclass(frozen=True)
class HarnessStep:
    """One thing the harness did. The shape `session_event(kind='tool_call')` already
    renders, so the UI needs nothing new."""

    tool: str
    detail: str
    outcome: str


@dataclass(frozen=True)
class HarnessRun:
    steps: list[HarnessStep] = field(default_factory=list)
    said: str = ""
    tokens: int = 0
    errors: list[str] = field(default_factory=list)
    # What the harness exited with, and whatever it printed that was not an event. A
    # harness that dies before its first JSON line -- a bad config, a provider it cannot
    # reach, a crash on startup -- otherwise leaves nothing at all: no steps, no text, no
    # error event, and a summary of "". That happened on the first real multi-item run
    # here, and the only way to find out why was to read the container by hand.
    exit_code: int | None = None
    noise: str = ""

    @property
    def tool_counts(self) -> Counter[str]:
        return Counter(step.tool for step in self.steps)

    def summary(self) -> str:
        """The bounded line that becomes the persona's own transcript turn.

        Written as the persona would say it, because that is what it becomes: this is
        replayed to the persona as something it said, and a log line in the first person
        reads as one.
        """
        if not self.steps and not self.said and not self.errors:
            return self._silent()
        parts: list[str] = []
        if self.steps:
            counts = self.tool_counts
            worked = ", ".join(f"{name} ×{n}" for name, n in counts.most_common(4))
            parts.append(f"{len(self.steps)} steps ({worked})")
        if self.tokens:
            parts.append(f"{self.tokens:,} tokens")
        head = " · ".join(parts)
        body = self.said.strip()
        if self.errors:
            body = f"{body}\n⚠ {self.errors[0]}".strip()
        text = f"{head}\n\n{body}".strip() if head else body
        return text[:_SUMMARY_CHARS] + ("…" if len(text) > _SUMMARY_CHARS else "")

    def _silent(self) -> str:
        """A run that reported nothing at all.

        Saying nothing here is the wrong answer: the transcript then shows a delegation
        that touched no files and failed its tests, with no indication that the agent
        never ran. The exit code and whatever it printed are both in the output already.
        """
        if self.exit_code is None:
            return ""
        if self.exit_code == 0:
            head = "The harness ran and reported nothing -- no edits, no commands."
        else:
            head = f"⚠ The harness exited {self.exit_code} without reporting a single step."
        text = f"{head}\n\n{self.noise}".strip() if self.noise else head
        return text[:_SUMMARY_CHARS] + ("…" if len(text) > _SUMMARY_CHARS else "")


def _segment(output: str) -> str:
    """Just the harness's own stdout, between its step marker and whatever came next."""
    start = output.find(_START)
    if start == -1:
        return ""
    rest = output[start + len(_START) :]
    ends = [rest.find(marker) for marker in _END_MARKERS]
    cut = min((index for index in ends if index != -1), default=-1)
    return rest if cut == -1 else rest[:cut]


def _exit_code(output: str) -> int | None:
    """What the harness exited with, from the marker the work script prints after it.

    None means the marker is absent, which is not the same as a clean run: the script
    never reached that line (the clone failed, the container died), and claiming an exit
    code for it would be an invention.
    """
    index = output.rfind(_RC_MARKER)
    if index == -1:
        return None
    tail = output[index + len(_RC_MARKER) :].split(maxsplit=1)
    if not tail:
        return None
    try:
        return int(tail[0].strip().strip('"'))
    except ValueError:
        return None


def _detail(state: dict[str, Any]) -> str:
    """The most useful one line about a step: what was run, or what was touched."""
    payload = state.get("input")
    if not isinstance(payload, dict):
        return ""
    for key in ("command", "filePath", "path", "pattern", "query"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_COMMAND_CHARS]
    return ""


def summarise(output: str) -> HarnessRun:
    """Read a harness run out of a work script's combined output.

    Tolerant on purpose: a line that is not JSON, or JSON in a shape this does not know,
    is skipped rather than raised on. The alternative is a delegation that did the work and
    then failed while describing it, which would be a poor trade.
    """
    steps: list[HarnessStep] = []
    said: list[str] = []
    errors: list[str] = []
    noise: list[str] = []
    tokens = 0

    for line in _segment(output).splitlines():
        line = line.strip()
        if not line.startswith("{"):
            if line:
                noise.append(line)
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        raw_part = event.get("part")
        part: dict[str, Any] = raw_part if isinstance(raw_part, dict) else {}

        if kind == "error":
            detail = event.get("error")
            message = ""
            if isinstance(detail, dict):
                data = detail.get("data")
                message = str((data or {}).get("message") or detail.get("name") or "")
            errors.append(message[:_COMMAND_CHARS] or "the harness reported an error")
        elif kind == "tool_use":
            raw_state = part.get("state")
            state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
            steps.append(
                HarnessStep(
                    tool=str(part.get("tool") or "tool"),
                    detail=_detail(state),
                    outcome=str(state.get("status") or "completed"),
                )
            )
        elif kind == "text":
            text = str(part.get("text") or "").strip()
            if text:
                said.append(text)
        elif kind in ("step_finish", "step-finish"):
            raw_usage = part.get("tokens")
            usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
            # Two shapes, both seen from opencode 1.18.33 in one run: a step that ends on
            # `stop` carries `total`, one that ends on `tool-calls` carries only the
            # components. Reading `total` alone counted a tool-calling run -- which is
            # every coding run -- as zero.
            reported = usage.get("total")
            if isinstance(reported, int) and reported > 0:
                tokens += reported
            else:
                for key in ("input", "output", "reasoning"):
                    part_count = usage.get(key)
                    if isinstance(part_count, int):
                        tokens += part_count

    # The last thing it said is its conclusion; earlier text is working-out.
    return HarnessRun(
        steps=steps,
        said=said[-1] if said else "",
        tokens=tokens,
        errors=errors,
        exit_code=_exit_code(output),
        # The *last* lines, not the first: a crash ends with the reason.
        noise="\n".join(noise[-4:])[-_NOISE_CHARS:],
    )
