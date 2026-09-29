"""How much knowledge a turn may carry -- the part the flow author cannot know.

A phase declares ``budget.max_tokens``, and that number is the right thing for a flow to
say: it encodes how much of this kind of turn should be recalled rather than reasoned.
What it cannot encode is the model. Every flow in every pack was authored against local
models with a small window, so a frontier model with a hundred and twenty-eight thousand
tokens of room is handed the same three thousand as an eight-billion-parameter model on a
laptop, and the difference is invisible to everyone: the flow is portable, which is the
point, and the connection is chosen later by someone who never reads it.

So the flow's number becomes a **floor** rather than the answer. A model whose window is
known gets a share of it, capped, and a model whose window is not known gets exactly what
the flow said -- the behaviour every deployment has today, which is the right thing to be
wrong towards.

**The share and the cap are starting points, not findings.** Bigger is not simply better:
a long context dilutes attention as well as informing it, and every token is paid for on
every turn of every persona. They are set low enough to be a clear improvement and small
enough to be safe, and they are here as named constants so that measuring can move them.

**``knowledge_token_budget`` on the connection wins outright.** The same shape as
``history_char_budget``, which already decides how much *transcript* a model is given for
exactly this reason -- an operator who has measured their own model should not have to
argue with a heuristic.
"""

from __future__ import annotations

from collections.abc import Mapping

# The parameter a connection or persona sets to decide this itself.
KNOWLEDGE_BUDGET_PARAM = "knowledge_token_budget"

# What fraction of a model's input window knowledge may occupy. The rest is the system
# blocks, the transcript, the tool schemas and the reply.
KNOWLEDGE_CONTEXT_SHARE = 0.15

# An upper bound whatever the window. A million-token model does not want a hundred and
# fifty thousand tokens of retrieved handbook on every turn of every persona, and nobody
# wants to discover that from an invoice.
KNOWLEDGE_TOKENS_CAP = 8000


def effective_knowledge_tokens(
    phase_tokens: int,
    *,
    params: Mapping[str, object] | None = None,
    context_window: int | None = None,
) -> int:
    """The knowledge budget for one turn: the connection's own number if it set one, else
    a capped share of the model's window, never less than the phase asked for."""
    raw = (params or {}).get(KNOWLEDGE_BUDGET_PARAM)
    if raw is not None:
        try:
            chosen = int(str(raw))
        except (TypeError, ValueError):
            chosen = 0
        if chosen > 0:
            return chosen
    if not context_window or context_window <= 0:
        return phase_tokens
    share = min(int(context_window * KNOWLEDGE_CONTEXT_SHARE), KNOWLEDGE_TOKENS_CAP)
    return max(phase_tokens, share)
