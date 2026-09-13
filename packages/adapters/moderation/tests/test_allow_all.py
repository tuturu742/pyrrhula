from adapters.moderation.allow_all import AllowAllModerationProvider
from core.ports.moderation import ModerationProvider


async def test_allows_everything() -> None:
    provider: ModerationProvider = AllowAllModerationProvider()
    result = await provider.check("anything at all", context="knowledge_entry")
    assert result.allowed is True
    assert result.reasons == []
