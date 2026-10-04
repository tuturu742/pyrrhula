"""Templates stay valid and in step with the harnesses; Propose drafts, meters, and
never writes."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent
from core.audit.models import UsageRecordRow
from core.harness.registry import BUILTIN_HARNESSES
from core.images.dockerfile import validate_dockerfile
from core.images.models import ImageDefinitionRow
from core.images.propose import propose_dockerfile, repository_context
from core.images.templates import list_templates
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


def test_every_template_passes_the_validator() -> None:
    for template in list_templates():
        report = validate_dockerfile(template["dockerfile"])
        assert report.ok, (template["key"], report.errors)


def test_harness_templates_follow_the_harnesses_rather_than_copying_them() -> None:
    """The install lines are appended at build time from BUILTIN_HARNESSES. A template
    that carried its own copy would go stale the day the version is bumped."""
    harness_templates = [t for t in list_templates() if t["harness_key"]]
    assert {t["harness_key"] for t in harness_templates} == {
        k
        for k, s in BUILTIN_HARNESSES.items()
        if all(str(c).startswith("npm ") for c in s.get("setup_cmds") or []) and s.get("setup_cmds")
    }
    for template in harness_templates:
        spec = BUILTIN_HARNESSES[template["harness_key"]]
        for cmd in spec["setup_cmds"]:
            assert cmd not in template["dockerfile"]
        assert "node" in template["dockerfile"].splitlines()[0], "npm needs Node"


def test_the_model_sees_paths_manifests_and_commands_only() -> None:
    tree = {
        "project.godot": 'config/features=PackedStringArray("4.3")',
        "scripts/main.gd": "extends Node\n# a long script the model does not need",
        "assets/cat.png": "",
    }
    context = repository_context(
        tree, test_cmd="godot --headless --script tests/run_tests.gd", setup_cmds=[], manifest={}
    )
    assert "scripts/main.gd" in context and "assets/cat.png" in context
    assert '"4.3"' in context, "a manifest's content is shown"
    assert "a long script" not in context, "other files contribute their path only"
    assert "godot --headless" in context


@dataclass
class _Provider:
    dockerfile: str
    rationale: str = "Godot for the tests, git to clone."

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def generate_structured(self, req: GenerationRequest, schema: type) -> Any:  # type: ignore[type-arg]
        assert req.purpose == "rewrite"
        return schema(dockerfile=self.dockerfile, rationale=self.rationale)

    def count_tokens(self, text: str, model: str) -> int:
        return max(len(text.split()), 1)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            supports_tools=False, supports_json_mode=True, supports_prompt_caching=False
        )


async def test_a_proposal_is_metered_checked_and_never_saved(db_available: None) -> None:
    tenant_id, owner, _ = await seed_dev_tenant(slug=f"propose-{uuid.uuid4().hex[:8]}")
    profile = await create_agent(
        tenant_id, "drafts", "echo", "echo-1", encryptor=IdentityEncryptor()
    )
    spec = {"key": "opencode", **BUILTIN_HARNESSES["opencode"]}
    draft = (
        "```dockerfile\nFROM docker.io/library/debian:bookworm\nCOPY . /src\n"
        "RUN npm i -g opencode-ai@1.18.33\n```"
    )
    proposal = await propose_dockerfile(
        tenant_id,
        context="Files:\nproject.godot",
        harness_spec=spec,
        agent=profile,
        provider=_Provider(draft),  # type: ignore[arg-type]
        api_key=None,
        principal_id=owner,
    )
    assert proposal.dockerfile.startswith("FROM docker.io/library/debian"), "fences stripped"
    assert any("build context" in e for e in proposal.errors), "the validator's verdict"
    assert any("installs the harness itself" in w for w in proposal.warnings)
    assert any("no Node" in w for w in proposal.warnings)
    async with tenant_scope(tenant_id) as session:
        metered = await session.scalar(
            select(func.count())
            .select_from(UsageRecordRow)
            .where(UsageRecordRow.tenant_id == tenant_id, UsageRecordRow.purpose == "rewrite")
        )
        saved = await session.scalar(
            select(func.count())
            .select_from(ImageDefinitionRow)
            .where(ImageDefinitionRow.tenant_id == tenant_id)
        )
    assert metered == 1
    assert saved == 0, "a proposal writes nothing"
