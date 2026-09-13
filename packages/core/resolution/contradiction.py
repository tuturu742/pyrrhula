"""Contradiction check (C1.7, plan §9.2 step 7/§16.4, Q4 resolved 2026-07-16): best-effort
scan of a reply against its turn's resolution records. Flags for a UI correction badge --
**no regeneration, ever**. The dice widget already renders the truth straight from
``ResolutionRecord`` (INV-7); a contradicting narration is cosmetic and self-correcting --
the reader sees the badge and the real number sits right there. That is a strictly
different, much lower-stakes failure mode than §8.7's secret-leak check (which *does*
regenerate) -- **do not generalise this module's no-regen posture to that one.**

**Conservative by design** (the task's own words: "prefer false negatives over noisy
flags"). Two independent, narrow signals, each only fired when unambiguous:

1. **Outcome words.** The reply mentions success-family or failure-family words, but not
   both (mentioning both is ambiguous prose -- "you don't fail, but only barely succeed" --
   and gets no signal at all, not a guess). Only compared against a record whose own
   ``outcome`` is literally ``"success"``/``"failure"`` -- a PbtA-style banded outcome
   (``"partial_success"`` etc) has no reliable word-level narration signature this simple
   lexicon can represent, so it's skipped entirely rather than risk a false positive.
2. **A stated total.** Only numbers appearing in a small set of roll-referencing patterns
   ("rolled a 15", "total of 15", "15 vs 12") count as a *claimed* total -- a stray digit
   elsewhere in the prose ("you have 15 gold") is never mistaken for one.

No model call, no I/O -- a regex scan, bounded and fast by construction (the acceptance
criterion's "<10ms typical" is a property of what this function *doesn't* do, not
something it has to be tuned to hit).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select

from core.resolution.records import ResolutionRecordRow
from core.sessions.models import MessageRow
from core.tenancy.scope import tenant_scope

_SUCCESS_WORDS = ("succeed", "succeeds", "succeeded", "success", "hit", "hits", "pass", "passes")
_FAILURE_WORDS = ("fail", "fails", "failed", "failure", "miss", "misses", "missed")

_TOTAL_PATTERN = re.compile(
    r"\btotal(?:\s+of)?\s+(?:is\s+)?(\d+)\b"
    r"|\brolled?(?:\s+an?)?\s+(?:a\s+)?(\d+)\b"
    r"|\b(\d+)\s+(?:vs\.?|versus)\s+\d+\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ContradictionFlag:
    record_id: uuid.UUID
    reason: str  # 'outcome_word_mismatch' | 'total_mismatch'
    detail: str


def _mentioned_outcome(text: str) -> str | None:
    lowered = text.lower()
    mentions_success = any(re.search(rf"\b{w}\b", lowered) for w in _SUCCESS_WORDS)
    mentions_failure = any(re.search(rf"\b{w}\b", lowered) for w in _FAILURE_WORDS)
    if mentions_success and not mentions_failure:
        return "success"
    if mentions_failure and not mentions_success:
        return "failure"
    return None  # neither, or both (ambiguous) -- no signal either way


def _mentioned_total(text: str) -> int | None:
    match = _TOTAL_PATTERN.search(text)
    if match is None:
        return None
    group = next(g for g in match.groups() if g is not None)
    return int(group)


def scan_for_contradictions(
    reply_text: str, records: list[ResolutionRecordRow]
) -> list[ContradictionFlag]:
    mentioned_outcome = _mentioned_outcome(reply_text)
    mentioned_total = _mentioned_total(reply_text)

    flags: list[ContradictionFlag] = []
    for record in records:
        if (
            mentioned_outcome is not None
            and record.outcome in ("success", "failure")
            and mentioned_outcome != record.outcome
        ):
            flags.append(
                ContradictionFlag(
                    record_id=record.id,
                    reason="outcome_word_mismatch",
                    detail=(
                        f"reply implies {mentioned_outcome!r} but the record says "
                        f"{record.outcome!r}"
                    ),
                )
            )
        elif mentioned_total is not None and mentioned_total != record.total:
            flags.append(
                ContradictionFlag(
                    record_id=record.id,
                    reason="total_mismatch",
                    detail=(
                        f"reply states {mentioned_total} but the record's total is {record.total}"
                    ),
                )
            )
    return flags


async def flag_contradictions(
    tenant_id: uuid.UUID, message_id: uuid.UUID, flags: list[ContradictionFlag]
) -> None:
    """Persists flags onto the message (§12.7's ``moderation_flags`` column) -- a no-op if
    ``flags`` is empty, leaving ``moderation_flags`` at its default ``{}}`` rather than
    writing an empty ``contradiction`` key (so "flagged" is exactly "the key is present
    and non-empty", not "the key exists but is empty")."""
    if not flags:
        return
    async with tenant_scope(tenant_id) as session:
        message = await session.get(MessageRow, message_id)
        assert message is not None
        message.moderation_flags = {
            **message.moderation_flags,
            "contradiction": [str(f.record_id) for f in flags],
        }


async def contradiction_rate(tenant_id: uuid.UUID, session_id: uuid.UUID) -> float:
    """§16.4: "measure the rate from the first session; under ~1% the badge is the
    permanent answer." Fraction of assistant messages in a session carrying a
    contradiction flag -- 0.0 on a session with no assistant messages yet, not a
    division-by-zero error."""
    async with tenant_scope(tenant_id) as db_session:
        total = await db_session.scalar(
            select(func.count(MessageRow.id)).where(
                MessageRow.session_id == session_id, MessageRow.role == "assistant"
            )
        )
        flagged = await db_session.scalar(
            select(func.count(MessageRow.id)).where(
                MessageRow.session_id == session_id,
                MessageRow.role == "assistant",
                MessageRow.moderation_flags.has_key("contradiction"),
            )
        )
    total = total or 0
    flagged = flagged or 0
    return (flagged / total) if total else 0.0
