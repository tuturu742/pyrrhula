"""ModelProvider port (D9, D14, §13.6). v1 wraps LiteLLM. The egress policy check (D14)
lives *inside* this port, not at call sites, keyed on ``purpose`` — so "which purposes
may reach a hosted provider" is enforced in exactly one place rather than becoming a
per-agent convenience setting that has to be remembered at every call site.

``ToolSpec``/``ToolCall`` (B1.7) added for the agent runtime's tool loop.
``GenerationRequest.tools`` is empty by default (no behavioural change for existing
callers that never pass any); ``Chunk.tool_calls`` is populated only on the terminal chunk
of a tool-calling turn (streaming tool-call argument fragments are accumulated by the
adapter, never surfaced to callers as partial/unparseable JSON).

``GenerationRequest.cache_boundary_index`` (C1.4, plan §6.3 step 9/§16.1): the index into
``messages`` up to and including which content is the *stable* prefix (system + persona +
entity schema + constant knowledge + rule system -- unchanging within a session, per
``core.assembler.layout.LayoutSections``). ``None`` means "no known boundary, don't mark
anything" -- the default, so existing callers that never set it see no behavioural change.
An adapter applies a provider-specific cache marker at that boundary only when
``capabilities(model).supports_prompt_caching`` is true; a provider with no caching
support simply ignores the boundary. ``Chunk.cached_tokens`` is the provider-reported
count of prompt tokens served from cache on a hit, populated (best-effort; not every
provider/streaming mode reports it) on the terminal chunk alongside ``finish_reason``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


class EgressDeniedError(Exception):
    """Raised when a tenant's ``egress_policy`` forbids ``purpose`` from reaching the
    requested model's provider kind (D14)."""


@dataclass(frozen=True)
class ToolSpec:
    """One tool the model may call, in the JSON-Schema-parameters shape LiteLLM
    normalises across providers (OpenAI/Anthropic/Gemini function-calling)."""

    name: str
    description: str
    parameters: dict[str, object]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class Chunk:
    text: str
    finish_reason: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    cached_tokens: int = 0


@dataclass(frozen=True)
class Capabilities:
    supports_tools: bool
    supports_json_mode: bool
    supports_prompt_caching: bool


@dataclass(frozen=True)
class GenerationRequest:
    model: str
    # dict values are str for plain messages, but tool-call turns carry
    # content=None and a tool_calls list (OpenAI shape) -- hence object.
    messages: list[dict[str, object]]
    # 'generation'|'gate'|'rerank'|'embed'|'report'|'rewrite' — same taxonomy as
    # usage_record.purpose (§12.8), so cost attribution and egress policy share one
    # vocabulary rather than inventing a second.
    purpose: str
    # Missing purpose key => permissive default ("all purposes allowed", per plan §13.9).
    egress_policy: Mapping[str, Sequence[str]] = field(default_factory=dict)
    temperature: float | None = None
    max_tokens: int | None = None
    tools: tuple[ToolSpec, ...] = ()
    cache_boundary_index: int | None = None
    # Per-``Agent`` override of the provider's endpoint (``agent.
    # api_base``) -- e.g. a specific Ollama host/port when a tenant runs more than one
    # local deployment. ``None`` means "use the provider's/LiteLLM's own default"
    # (an env var like ``OLLAMA_API_BASE``, or the hosted-provider default) -- no
    # behavioural change for any existing caller that never sets it.
    api_base: str | None = None
    # Per-``Agent`` provider credential (decrypted from ``agent.credential_ref`` at call
    # time), passed straight to the provider SDK. ``None`` means "no key supplied" -- fine
    # for a local provider (Ollama), and for a cloud provider it falls back to the process
    # env (e.g. ANTHROPIC_API_KEY) exactly as before. Never logged, never persisted.
    api_key: str | None = None


class ModelProvider(Protocol):
    def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]: ...

    async def generate_structured(self, req: GenerationRequest, schema: type[ModelT]) -> ModelT: ...

    def count_tokens(self, text: str, model: str) -> int: ...

    def capabilities(self, model: str) -> Capabilities: ...


def provider_kind(model: str) -> str:
    """'local' if the model string routes to a local runtime -- Ollama via LiteLLM's
    ``ollama/`` prefix, or a directly-loaded self-hosted model via the ``local/`` prefix
    (A1.3: embedding models, which don't go through LiteLLM at all) -- else 'cloud'.
    Shared by ``GenerationRequest`` and ``EmbedRequest`` so egress policy uses one
    classifier regardless of purpose."""
    return "local" if model.startswith(("ollama/", "local/")) else "cloud"


def check_egress(purpose: str, model: str, egress_policy: Mapping[str, Sequence[str]]) -> None:
    """D14. An absent ``purpose`` key is permissive by default — the plan is explicit
    that the default policy allows everything; egress becomes restrictive only once a
    tenant states a policy."""
    allowed = egress_policy.get(purpose)
    if allowed is None:
        return
    kind = provider_kind(model)
    if kind not in allowed:
        raise EgressDeniedError(
            f"purpose {purpose!r} may not reach a {kind!r} provider (model={model!r}); "
            f"tenant policy for {purpose!r} allows: {list(allowed)!r}"
        )
