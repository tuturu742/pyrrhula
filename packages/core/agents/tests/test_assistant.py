"""The workspace assistant: required, idempotent to ensure, and metered when it drafts.
Live-Postgres tests with a scripted ``ModelProvider`` double, matching
``test_editing``'s pattern (the assistant generalises those proposal flows)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from adapters.embedding.stub.provider import StubEmbeddingProvider
from core.agents.assistant import (
    ASSISTANT_KEY,
    UnknownAssistTaskError,
    assist,
    ensure_workspace_assistant,
)
from core.agents.authoring import get_agent
from core.agents.models import Agent
from core.audit.models import UsageRecordRow
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


@dataclass
class _ScriptedAssistProvider:
    text: str
    seen_purposes: list[str]

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        raise NotImplementedError
        yield  # pragma: no cover -- makes this an async generator for the Protocol

    async def generate_structured(self, req: GenerationRequest, schema: type) -> object:  # type: ignore[type-arg]
        self.seen_purposes.append(req.purpose)
        return schema(text=self.text)

    def count_tokens(self, text: str, model: str) -> int:
        return max(len(text.split()), 1)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            supports_tools=False, supports_json_mode=True, supports_prompt_caching=False
        )


async def test_ensure_workspace_assistant_is_idempotent(db_available: None) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"assist-ensure-{uuid.uuid4().hex[:8]}"
    )

    first = await ensure_workspace_assistant(tenant_id, workspace_id)
    second = await ensure_workspace_assistant(tenant_id, workspace_id)

    assert first.id == second.id
    assert first.key == ASSISTANT_KEY
    assert first.persona_type == "informational"
    assert (await get_agent(tenant_id, first.agent_id)) is not None

    async with tenant_scope(tenant_id) as session:
        profiles = (
            await session.execute(
                select(Agent).where(Agent.tenant_id == tenant_id, Agent.name == "Assistant model")
            )
        ).scalars()
        assert len(list(profiles)) == 1


async def test_assist_drafts_and_meters(db_available: None) -> None:
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=f"assist-draft-{uuid.uuid4().hex[:8]}"
    )
    persona = await ensure_workspace_assistant(tenant_id, workspace_id)
    profile = await get_agent(tenant_id, persona.agent_id)
    assert profile is not None

    async with tenant_scope(tenant_id) as session:
        viewer = await session.get(Principal, owner_id)
        assert viewer is not None
        session.expunge(viewer)

    provider = _ScriptedAssistProvider(text="You are a stern chronicler.", seen_purposes=[])
    result = await assist(
        tenant_id,
        workspace_id,
        viewer,
        task="draft_persona",
        subject="Chronicler",
        instruction="stern, keeps records",
        profile=profile,
        provider=provider,
        embedder=StubEmbeddingProvider(dimension=1024),
    )

    assert result.text == "You are a stern chronicler."
    assert provider.seen_purposes == ["rewrite"]

    async with tenant_scope(tenant_id) as session:
        purposes = [
            r.purpose
            for r in (
                await session.execute(
                    select(UsageRecordRow).where(UsageRecordRow.tenant_id == tenant_id)
                )
            ).scalars()
        ]
    assert "rewrite" in purposes


async def test_assist_rejects_unknown_task(db_available: None) -> None:
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=f"assist-task-{uuid.uuid4().hex[:8]}"
    )
    persona = await ensure_workspace_assistant(tenant_id, workspace_id)
    profile = await get_agent(tenant_id, persona.agent_id)
    assert profile is not None
    async with tenant_scope(tenant_id) as session:
        viewer = await session.get(Principal, owner_id)
        assert viewer is not None
        session.expunge(viewer)

    with pytest.raises(UnknownAssistTaskError):
        await assist(
            tenant_id,
            workspace_id,
            viewer,
            task="write_my_thesis",
            subject="",
            instruction="",
            profile=profile,
            provider=_ScriptedAssistProvider(text="", seen_purposes=[]),
            embedder=StubEmbeddingProvider(dimension=1024),
        )


async def test_the_context_budget_is_a_workspace_setting(
    db_available: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assistant's retrieval budget was a module constant at 2400 tokens, chosen when a
    local Ollama model ran at a 4096-token context. That floor is gone, and 2400 was
    measurably too small once a repository was in the workspace: `misc` is where every
    source file lands and takes the smallest share of the class split, so "how does this
    code fit together" was answered from one chunk of one file.

    The right value is a property of the workspace -- a six-crate codebase and a one-page
    handbook do not want the same budget -- so it resolves through the settings chain, and
    a workspace that sets it must win over the platform default.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from core.agents import assistant as assistant_module
    from core.tenancy.models import Workspace

    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=f"assist-budget-{uuid.uuid4().hex[:8]}"
    )
    persona = await ensure_workspace_assistant(tenant_id, workspace_id)
    profile = await get_agent(tenant_id, persona.agent_id)
    assert profile is not None

    async with tenant_scope(tenant_id) as session:
        viewer = await session.get(Principal, owner_id)
        assert viewer is not None
        session.expunge(viewer)

    seen: list[int] = []
    real_context = assistant_module._workspace_context

    async def _spy(*args: object, **kwargs: object):  # noqa: ANN202
        seen.append(int(args[5] if len(args) > 5 else kwargs["max_tokens"]))  # type: ignore[arg-type]
        return await real_context(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(assistant_module, "_workspace_context", _spy)

    async def _run() -> None:
        await assist(
            tenant_id,
            workspace_id,
            viewer,
            task="ask",
            subject="loxia",
            instruction="which crate talks to the server?",
            profile=profile,
            provider=_ScriptedAssistProvider(text="ok", seen_purposes=[]),
            embedder=StubEmbeddingProvider(dimension=1024),
        )

    await _run()
    assert seen == [assistant_module._DEFAULT_CONTEXT_MAX_TOKENS]

    async with tenant_scope(tenant_id) as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.settings = {
            **(workspace.settings or {}),
            assistant_module._CONTEXT_MAX_TOKENS_SETTING: 12000,
        }
        flag_modified(workspace, "settings")
    await _run()
    assert seen[-1] == 12000, "a workspace's own budget must beat the platform default"


async def test_a_workspace_can_reweight_the_class_split(db_available: None) -> None:
    """`priority_weight` cannot express this, which is why the ratios are settable too.

    That weight is per attached *source*. A workspace whose knowledge is one repository
    carries the same weight into all three classes, and a uniform weight normalises away to
    no change whatsoever -- it says "this source matters more than that one", never "code
    matters more than prose here". The second is what a workspace with a codebase needs:
    every source file lands in `misc`, and the shipped split gives `misc` the least.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from core.agents import assistant as assistant_module
    from core.knowledge.retrieval.budget import split_budget
    from core.tenancy.models import Workspace

    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"assist-ratio-{uuid.uuid4().hex[:8]}"
    )

    shipped = await assistant_module._class_ratios(tenant_id, workspace_id)
    assert shipped == assistant_module._DEFAULT_CLASS_RATIOS

    async with tenant_scope(tenant_id) as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.settings = {
            **(workspace.settings or {}),
            assistant_module._CLASS_RATIOS_SETTING: {"rules": 0.2, "lore": 0.2, "misc": 0.6},
        }
        flag_modified(workspace, "settings")

    code_heavy = await assistant_module._class_ratios(tenant_id, workspace_id)
    assert split_budget(code_heavy, 6000)["misc"] == 3600
    assert split_budget(shipped, 6000)["misc"] == 1500


async def test_an_unusable_ratio_setting_falls_back_instead_of_starving_retrieval(
    db_available: None,
) -> None:
    """A total of zero would hand every class a zero budget, which surfaces as "the
    assistant stopped finding anything" rather than as a bad setting -- so it is ignored."""
    from sqlalchemy.orm.attributes import flag_modified

    from core.agents import assistant as assistant_module
    from core.tenancy.models import Workspace

    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"assist-bad-{uuid.uuid4().hex[:8]}"
    )
    for broken in ({"rules": 0, "lore": 0, "misc": 0}, {"rules": "lots"}, {}):
        async with tenant_scope(tenant_id) as session:
            workspace = await session.get(Workspace, workspace_id)
            assert workspace is not None
            workspace.settings = {assistant_module._CLASS_RATIOS_SETTING: broken}
            flag_modified(workspace, "settings")
        assert (
            await assistant_module._class_ratios(tenant_id, workspace_id)
            == assistant_module._DEFAULT_CLASS_RATIOS
        ), f"{broken!r} should have fallen back"
