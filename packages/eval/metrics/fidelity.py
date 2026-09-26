"""`behavioral_fidelity` : a blind pairwise LLM judge. Does malice=80
read more malicious than malice=20? If the judge can't tell the two transcripts apart,
the axis is cosmetic -- cut it, don't keep shipping a slider nobody can perceive.

**Blind is the whole point.** The judge receives two transcripts labeled arbitrarily "A"
and "B" and a plain-language description of the *trait* being judged (an axis's
`semantics_md`, never its numeric value) -- never which arm produced which transcript,
never the axis value, never which one the harness expects to "win". A judge that could
infer the answer from a label rather than the dialogue would make this metric
meaningless, not just biased.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict

from core.ports.model_provider import GenerationRequest, ModelProvider

_VALID_LABELS = frozenset({"A", "B"})


class FidelityJudgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    more_pronounced_transcript: str
    confidence: float


def build_judge_request(
    transcript_a: str, transcript_b: str, trait_description: str, model: str
) -> GenerationRequest:
    """The one place the judge's payload is assembled -- inspected directly by
    `test_fidelity_judge_inputs_are_blind` for the absence of any axis value or arm
    label. `trait_description` is a plain-language axis `semantics_md`, not its key or
    numeric value -- "how willing the agent is to actively mislead", never
    "deception_propensity=80"."""
    system = (
        "You will read two short roleplay transcripts, labeled A and B. Judge which "
        "one reads as more strongly exhibiting this trait, based only on the dialogue "
        f"itself: {trait_description}\n"
        "Respond with which transcript (A or B) shows the trait more strongly, and "
        "your confidence."
    )
    payload = f"Transcript A:\n{transcript_a}\n\nTranscript B:\n{transcript_b}"
    return GenerationRequest(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": payload},
        ],
        # 'report': CLAUDE.md rule 11's purpose taxonomy has no 'judge' entry, and this
        # call scores quality for the eval *report*, not a disclosure decision -- the
        # closest existing fit, not a new taxonomy value invented for the harness.
        purpose="report",
        max_tokens=150,
    )


async def judge_pair(
    transcript_a: str,
    transcript_b: str,
    trait_description: str,
    *,
    provider: ModelProvider,
    model: str,
) -> FidelityJudgeResponse:
    request = build_judge_request(transcript_a, transcript_b, trait_description, model)
    response = await provider.generate_structured(request, FidelityJudgeResponse)
    if response.more_pronounced_transcript not in _VALID_LABELS:
        raise ValueError(
            f"judge returned an invalid label {response.more_pronounced_transcript!r}, "
            f"must be one of {sorted(_VALID_LABELS)}"
        )
    return response


JudgeFn = Callable[[str, str, str], Awaitable[FidelityJudgeResponse]]


async def behavioral_fidelity(
    high_transcript: str,
    low_transcript: str,
    trait_description: str,
    *,
    judge: JudgeFn,
) -> float:
    """1.0 if the judge correctly identifies `high_transcript` (the higher axis value)
    as more pronounced than `low_transcript`, 0.0 if it gets it backwards -- a single
    pair's score; a real harness run averages this across many scenario/axis pairs.
    Takes a `judge` callback (same injection-seam shape as the `regenerate`) so the
    A/B assignment and the actual provider call are the caller's composition, not baked
    in here -- this function only knows "high" and "low", never which label the caller
    assigned to which."""
    response = await judge(high_transcript, low_transcript, trait_description)
    return 1.0 if response.more_pronounced_transcript == "A" else 0.0
