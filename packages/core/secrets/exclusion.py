"""Context exclusion (INV-8): assembler step 8 made
real. Per gate decision, **conceal** removes the secret's plaintext from the
generation context entirely and injects `behavioral_directive` instead; **hint** injects
`hint_text` (+ directive), plaintext still absent; **reveal_full** injects `content` and
commits the world-changing side effects (disclosure event + holder update) atomically.

"The single most important design claim" : exclusion happens at *selection*, not by
post-hoc scrubbing. `render_injection` never has a `content` field to reach for unless
the action is `reveal_full` -- a concealed secret's fact is structurally absent from this
module's own output, not redacted out of a buffer that once held it.

`core.assembler.context_assembler` is this module's sole caller; it already holds INV-1's
secrets-repo import right (fetches `SecretRow`, decrypts `content` on reveal) and hands
this module fully-resolved plain data -- this module never imports `core.secrets.repo`
itself and doesn't need to (this task doesn't touch the allowlist).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from core.secrets.decisions import record_reveal
from core.secrets.models import SecretDisclosureEventRow

_VALID_ACTIONS = frozenset({"conceal", "hint", "reveal_full"})


@dataclass(frozen=True)
class ResolvedSecretDecision:
    """One secret's fully-resolved disposition for this turn, as the assembler already
    has it in hand -- `content` is only ever populated (already decrypted, by whoever
    called `core.secrets.repo.get_secret_plaintext`-equivalent) when `action ==
    'reveal_full'`; for `conceal`/`hint` it stays `None`, so there is no plaintext for
    this module to accidentally reach for even by mistake."""

    secret_id: uuid.UUID
    action: str
    decision_id: uuid.UUID | None
    content: str | None
    hint_text: str | None
    behavioral_directive: str | None
    disclosed_to_principal_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class ExclusionRedaction:
    type: str
    id: str
    reason: str


def render_injection(resolved: ResolvedSecretDecision) -> tuple[str, ExclusionRedaction | None]:
    """No decision at all, or an unrecognised action, defaults to conceal -- the
    assembler enforces fail-closed independently of E2.5 doing the same (this task's own
    subtask); a bug that let an unvalidated action string through must not become a leak."""
    # EVAL ARMS ONLY (arms 1-2), checked BEFORE the fail-closed normalizer on
    # purpose -- these are the deliberately-broken designs the benchmark must measure:
    # plaintext in context WITH an instruction to keep it secret. Only
    # `core.assembler.secrets_gate_factory.resolve_turn_secrets` constructs this action,
    # only when its `eval_arm` parameter was explicitly passed (never over HTTP; the
    # architecture fence test enforces both). The redaction reason is honest so the
    # record shows exactly what happened.
    if resolved.action == "eval_expose":
        return resolved.content or "", ExclusionRedaction(
            type="secret", id=str(resolved.secret_id), reason="eval_expose"
        )

    # TRUST MODE: the holder's own brief, included structurally because the workspace
    # chose to rely on the acting model's judgement instead of a per-turn gate. Only the
    # holder's context ever receives this -- who-knows-what stays enforced by the holder
    # query; what-to-say is delegated to the model, which is the mode's entire meaning.
    # The redaction record is an honest marker of that choice, same as the eval arms.
    if resolved.action == "trusted":
        parts = [p for p in (resolved.content, resolved.behavioral_directive) if p]
        return "\n\n".join(parts), ExclusionRedaction(
            type="secret", id=str(resolved.secret_id), reason="trusted_to_model"
        )

    action = resolved.action if resolved.action in _VALID_ACTIONS else "conceal"

    if action == "reveal_full":
        return resolved.content or "", None

    if action == "hint":
        parts = [p for p in (resolved.hint_text, resolved.behavioral_directive) if p]
        text = " ".join(parts)
        return text, ExclusionRedaction(type="secret", id=str(resolved.secret_id), reason="hinted")

    # conceal (or any invalid/absent action, per the fail-closed default above)
    text = resolved.behavioral_directive or ""
    reason = "concealed" if resolved.action == "conceal" else "no_decision_default_conceal"
    return text, ExclusionRedaction(type="secret", id=str(resolved.secret_id), reason=reason)


async def apply_reveal(
    tenant_id: uuid.UUID,
    resolved: ResolvedSecretDecision,
    session_id: uuid.UUID,
    event_seq: int,
    *,
    disclosed_by_principal_id: uuid.UUID | None,
    message_id: uuid.UUID | None = None,
) -> SecretDisclosureEventRow:
    """The reveal path's atomic side effect: the disclosure event and the extended
    holder set commit together (`core.secrets.decisions.record_reveal`) -- subsequent
    turns' visibility reflects the new holder set immediately, since the world changed
    the moment the fact was spoken ('s "the reveal updates ACLs because disclosure
    changes the world"). The caller (context_assembler) is responsible for actually
    calling this only when `resolved.action == 'reveal_full'`."""
    return await record_reveal(
        tenant_id,
        resolved.secret_id,
        session_id,
        event_seq,
        resolved.decision_id,
        disclosed_by_principal_id=disclosed_by_principal_id,
        new_holder_principal_ids=resolved.disclosed_to_principal_ids,
        message_id=message_id,
    )
