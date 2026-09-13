"""Post-generation leak check (E2.7, plan §8.4 step 5, §16.4): defence in depth behind
exclusion (E2.6), not the control. `unauthorized_disclosure_rate == 0` must hold from
exclusion alone -- E2.8's eval harness treats a nonzero value as a P0 assembler bug, not
something this check is meant to paper over. This module exists for what exclusion
structurally can't see: hint drift, a model inferring the fact from a too-specific
directive, an author-written directive that gives the game away.

**Q4's badge decision explicitly does not apply here.** Contradicted *narration*
(`core.resolution.contradiction`, C1.7) gets a UI badge and no regeneration -- a wrong
dice-total claim is cosmetic and self-correcting (the widget renders the truth straight
from `ResolutionRecord`, INV-7). A *leak* is not cosmetic and does not self-correct: once
a fact is in a delivered reply, it's disclosed, irreversibly. The loss profiles differ
(style bug vs. irreversible disclosure), so the response differs: regenerate once, then
replace with a fallback and alert the overseer -- never a badge, never a silent pass.

Deliberately free of `core.secrets.repo`: `ConcealedSecret.content` is plaintext the
caller (eventually `core.agents.runtime`'s turn loop) already resolved and decrypted --
this module never touches the secrets tables itself.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from core.ports.embedding import EmbeddingProvider, EmbedRequest
from core.sessions.models import SessionEventRow
from core.tenancy.scope import tenant_scope

_WORD_PATTERN = re.compile(r"[a-z0-9']+")
_LEAK_MIN_SHARED_WORDS = 6
_FALLBACK_REPLY = "I'd rather not get into that right now."


@dataclass(frozen=True)
class ConcealedSecret:
    secret_id: uuid.UUID
    content: str


@dataclass(frozen=True)
class LeakCheckResult:
    leaked_secret_ids: tuple[uuid.UUID, ...]

    @property
    def leaked(self) -> bool:
        return len(self.leaked_secret_ids) > 0


@dataclass(frozen=True)
class PostGenerationOutcome:
    final_content: str
    outcome: str  # 'clean' | 'regenerated' | 'fallback'
    regenerated: bool
    fallback_used: bool
    leaked_secret_ids: tuple[uuid.UUID, ...]


RegenerateFn = Callable[[], Awaitable[str]]


def _words(text: str) -> list[str]:
    return _WORD_PATTERN.findall(text.lower())


def _fuzzy_leak(content: str, reply_text: str) -> bool:
    """Same shape as `core.secrets.drafting`'s plaintext-leak detector (E2.2): a
    contiguous run of shared words, not raw characters -- paraphrase-with-identical-
    words is the actual risk, not an incidental short substring."""
    content_words = _words(content)
    reply_words = _words(reply_text)
    if len(content_words) < _LEAK_MIN_SHARED_WORDS:
        return False
    content_ngrams = {
        tuple(content_words[i : i + _LEAK_MIN_SHARED_WORDS])
        for i in range(len(content_words) - _LEAK_MIN_SHARED_WORDS + 1)
    }
    reply_ngrams = {
        tuple(reply_words[i : i + _LEAK_MIN_SHARED_WORDS])
        for i in range(len(reply_words) - _LEAK_MIN_SHARED_WORDS + 1)
    }
    return not content_ngrams.isdisjoint(reply_ngrams)


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = float(sum(x * y for x, y in zip(a, b, strict=True)))
    norm_a = float(sum(x * x for x in a) ** 0.5)
    norm_b = float(sum(y * y for y in b) ** 0.5)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def check_for_leak(
    reply_text: str,
    concealed_secrets: Sequence[ConcealedSecret],
    *,
    embedding_provider: EmbeddingProvider,
    tau_leak: float = 0.75,
) -> LeakCheckResult:
    """Fuzzy token-overlap OR embedding similarity above `tau_leak` against each
    concealed secret's *content* -- either signal alone is enough to flag; this errs
    toward over-flagging (defence in depth), unlike the disclosure gate's prefilter
    (E2.5), which deliberately errs the other way (a false skip there costs the plot; a
    false flag here costs one regeneration)."""
    if not concealed_secrets:
        return LeakCheckResult(leaked_secret_ids=())

    texts = [reply_text, *(s.content for s in concealed_secrets)]
    vectors = await embedding_provider.embed(
        EmbedRequest(model=embedding_provider.model_name, texts=texts)
    )
    reply_vector, content_vectors = vectors[0], vectors[1:]

    leaked: list[uuid.UUID] = []
    for secret, content_vector in zip(concealed_secrets, content_vectors, strict=True):
        fuzzy_hit = _fuzzy_leak(secret.content, reply_text)
        embedding_hit = _cosine_similarity(reply_vector, content_vector) > tau_leak
        if fuzzy_hit or embedding_hit:
            leaked.append(secret.secret_id)
    return LeakCheckResult(leaked_secret_ids=tuple(leaked))


async def _write_overseer_alert(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    event_seq: int,
    leaked_secret_ids: Sequence[uuid.UUID],
) -> None:
    async with tenant_scope(tenant_id) as session:
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                kind="secret_leak_alert",
                payload={"secret_ids": [str(s) for s in leaked_secret_ids]},
            )
        )


async def run_post_generation_check(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    event_seq: int,
    reply_text: str,
    concealed_secrets: Sequence[ConcealedSecret],
    *,
    embedding_provider: EmbeddingProvider,
    regenerate: RegenerateFn,
    tau_leak: float = 0.75,
) -> PostGenerationOutcome:
    """Regenerate-once, then fallback (§8.4 step 5): `regenerate` is called at most once,
    regardless of outcome -- a concealed agent that keeps leaking gets replaced with a
    bland in-voice deflection and an overseer alert, never a third generation attempt."""
    if not concealed_secrets:
        return PostGenerationOutcome(
            final_content=reply_text,
            outcome="clean",
            regenerated=False,
            fallback_used=False,
            leaked_secret_ids=(),
        )

    first = await check_for_leak(
        reply_text, concealed_secrets, embedding_provider=embedding_provider, tau_leak=tau_leak
    )
    if not first.leaked:
        return PostGenerationOutcome(
            final_content=reply_text,
            outcome="clean",
            regenerated=False,
            fallback_used=False,
            leaked_secret_ids=(),
        )

    regenerated_text = await regenerate()
    second = await check_for_leak(
        regenerated_text,
        concealed_secrets,
        embedding_provider=embedding_provider,
        tau_leak=tau_leak,
    )
    if not second.leaked:
        return PostGenerationOutcome(
            final_content=regenerated_text,
            outcome="regenerated",
            regenerated=True,
            fallback_used=False,
            leaked_secret_ids=(),
        )

    await _write_overseer_alert(tenant_id, session_id, event_seq, second.leaked_secret_ids)
    return PostGenerationOutcome(
        final_content=_FALLBACK_REPLY,
        outcome="fallback",
        regenerated=True,
        fallback_used=True,
        leaked_secret_ids=second.leaked_secret_ids,
    )
