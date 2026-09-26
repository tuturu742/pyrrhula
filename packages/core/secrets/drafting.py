"""AI-assisted drafting: given a secret's plaintext content, propose a
behavioral directive and a bounded hint the author can accept, edit, or discard.
`purpose='rewrite'` (the metering taxonomy) — a distinct, sanctioned reason to send secret
content to a model, separate from the disclosure gate's gists-only path and from generation.

Draft-and-approve, not autopilot: this module only ever proposes.
Nothing here writes to `secret` — accepting a proposal is a separate call to
`core.secrets.authoring.update_secret_fields`, made by the caller (the API route), not by
this module. The one write this module performs is its own `usage_record`, since a model
call happened (and cost something) whether or not the draft is ever accepted.
"""

from __future__ import annotations

import re
import time
import uuid

from pydantic import BaseModel

from core.agents.models import Agent
from core.audit.models import UsageRecordRow
from core.ports.model_provider import GenerationRequest, ModelProvider
from core.tenancy.egress import load_egress_policy
from core.tenancy.scope import tenant_scope

_PURPOSE = "rewrite"

# A verbatim (or near-verbatim) run of `content` leaking into the draft is exactly what
# this module exists to catch before it's ever shown to the author, let alone saved.
# Word-boundary tokenization, case-insensitive: fires on a contiguous run of this many
# shared words, not raw characters, since paraphrase-with-identical-words ("the vault
# code is 4471" verbatim) is the actual risk, not incidental short substrings.
_LEAK_MIN_SHARED_WORDS = 6
_WORD_PATTERN = re.compile(r"[a-z0-9']+")


class SecretDraftResult(BaseModel):
    behavioral_directive: str
    hint_text: str


class DraftContainsPlaintextError(Exception):
    """Raised when the model's own draft echoes a long-enough run of `content` verbatim.
    The caller (API route) surfaces this as a visible warning and discards the draft —
    nothing is ever saved from a rejected draft."""


def _words(text: str) -> list[str]:
    return _WORD_PATTERN.findall(text.lower())


def contains_plaintext_leak(content: str, draft_text: str) -> bool:
    content_words = _words(content)
    draft_words = _words(draft_text)
    if len(content_words) < _LEAK_MIN_SHARED_WORDS:
        return False
    content_ngrams = {
        tuple(content_words[i : i + _LEAK_MIN_SHARED_WORDS])
        for i in range(len(content_words) - _LEAK_MIN_SHARED_WORDS + 1)
    }
    draft_ngrams = {
        tuple(draft_words[i : i + _LEAK_MIN_SHARED_WORDS])
        for i in range(len(draft_words) - _LEAK_MIN_SHARED_WORDS + 1)
    }
    return not content_ngrams.isdisjoint(draft_ngrams)


def _build_request(
    content: str,
    model: str,
    api_base: str | None,
    egress_policy: dict[str, list[str]] | None = None,
) -> GenerationRequest:
    system = (
        "You draft two short pieces of text for a tabletop/narrative game secret, given "
        "its private content. (1) A behavioral directive: how a character who knows this "
        "fact should act, speak, or react around it -- WITHOUT stating or paraphrasing "
        "the fact itself. (2) A hint: a short, vague phrase that gestures at the topic "
        "without revealing the fact, suitable for a partial disclosure. Never quote or "
        "closely paraphrase the input content. Respond with the two fields only."
    )
    return GenerationRequest(
        egress_policy=egress_policy or {},
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"Secret content:\n{content}"},
        ],
        purpose=_PURPOSE,
        max_tokens=400,
        api_base=api_base,
    )


async def draft_directive_and_hint(
    tenant_id: uuid.UUID,
    content: str,
    *,
    agent: Agent,
    provider: ModelProvider,
    workspace_id: uuid.UUID | None = None,
) -> SecretDraftResult:
    """Calls the model, meters the call (`usage_record`, `purpose='rewrite'`) regardless
    of outcome -- it cost money whether or not the draft turns out usable -- then
    validates the result and raises `DraftContainsPlaintextError` if it leaks `content`
    verbatim, discarding the draft before it's ever returned to a caller."""
    model_string = f"{agent.provider}/{agent.model}"
    req = _build_request(content, model_string, agent.api_base, await load_egress_policy(tenant_id))

    start = time.monotonic()
    result = await provider.generate_structured(req, SecretDraftResult)
    latency_ms = int((time.monotonic() - start) * 1000)

    prompt_tokens = sum(
        provider.count_tokens(str(m.get("content") or ""), model_string) for m in req.messages
    )
    completion_tokens = provider.count_tokens(
        result.behavioral_directive + " " + result.hint_text, model_string
    )

    async with tenant_scope(tenant_id) as session:
        session.add(
            UsageRecordRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                agent_id=agent.id,
                provider=agent.provider,
                model=agent.model,
                purpose=_PURPOSE,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
            )
        )

    if contains_plaintext_leak(content, result.behavioral_directive) or contains_plaintext_leak(
        content, result.hint_text
    ):
        raise DraftContainsPlaintextError(
            "the drafted text echoes the secret's plaintext content verbatim; discarded"
        )

    return result
