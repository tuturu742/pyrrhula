"""Model-backed ModerationProvider: a classifier prompt through the
existing ``ModelProvider`` port -- real judgment without new infrastructure, local
ollama by default and anything the deployment configures otherwise.

Configuration (deployment-level, ``core.config.Settings``):

- ``PYRRHULA_MODERATION_MODEL`` e.g. ``ollama/qwen3.8:27b`` (empty = adapter unused;
  the factory keeps returning allow-all)
- ``PYRRHULA_MODERATION_API_BASE`` e.g. ``http://ollama:11434``

Failure posture: **fail-open with a flag** -- moderation here is the advisory tier;
withholding every message because the moderation model is down would make the
moderation service the availability ceiling of the whole product. The failure reason
is carried in the result so the hook's audit row records that the check was degraded,
which is not the same as silently passing.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from core.ports.model_provider import GenerationRequest, ModelProvider
from core.ports.moderation import ModerationResult

_SYSTEM = (
    "You are a content-safety classifier for a multi-agent collaboration platform. "
    "Assess ONLY genuinely harmful content: credible threats or incitement of violence "
    "against real people, sexual content involving minors, concrete operational "
    "instructions for serious harm (weapons, intrusion, large-scale fraud), and "
    "targeted harassment of real individuals. FICTION IS IN SCOPE ONLY WHEN IT IS A "
    "THIN WRAPPER FOR REAL-WORLD HARM: this platform hosts tabletop-RPG play where "
    "villains scheme, characters fight, and dark themes are legitimate -- narrative "
    "violence, in-world crimes, and morally grey roleplay are ALLOWED. Respond with "
    "the exact JSON schema requested."
)


class ModerationVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reasons: list[str]


@dataclass
class ModelBackedModerationProvider:
    provider: ModelProvider
    model: str  # full "provider/model" string
    api_base: str | None = None

    async def check(self, text: str, *, context: str) -> ModerationResult:
        request = GenerationRequest(
            model=self.model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Context: {context}\n\nContent to assess:\n---\n{text[:8000]}\n---"
                    ),
                },
            ],
            purpose="gate",
            max_tokens=200,
            api_base=self.api_base,
        )
        try:
            verdict = await self.provider.generate_structured(request, ModerationVerdict)
        except Exception as exc:  # noqa: BLE001 -- advisory tier fails open, flagged
            return ModerationResult(
                allowed=True, reasons=[f"moderation_degraded: {str(exc)[:200]}"]
            )
        return ModerationResult(allowed=verdict.allowed, reasons=list(verdict.reasons))
