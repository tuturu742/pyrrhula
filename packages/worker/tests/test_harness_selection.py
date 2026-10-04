"""Choosing a harness for a delegation.

A persona names a harness *key*. What that key means is the tenant's, and it can change
between one delegation and the next -- a licence lapses, a policy lands. So the question
these cover is not "does the persona's choice get used" but "is it re-checked", and what
happens when the answer is no.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from adapters.encryptor.identity import IdentityEncryptor
from core.harness.registry import register_harness, withhold_harness
from core.tenancy.seed import seed_dev_tenant
from worker.delegation import _apply_harness, _harness_config, _run_timeout


@dataclass
class _Assignee:
    id: uuid.UUID
    agent_id: uuid.UUID
    name: str = "Dev"
    harness: str = ""


async def _tenant_with_connection(slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    from core.agents.authoring import create_agent

    tenant_id, _, _ = await seed_dev_tenant(slug=slug)
    agent = await create_agent(
        tenant_id,
        name="conn",
        provider="openai",
        model="gpt-nano",
        encryptor=IdentityEncryptor(),
    )
    return tenant_id, agent.id


async def test_a_persona_with_no_harness_gets_none(db_available: None) -> None:
    """The default, and the one that must not break: no harness means the existing
    one-shot codegen path, untouched."""
    tenant_id, agent_id = await _tenant_with_connection(f"hs-none-{uuid.uuid4().hex[:8]}")
    assignee = _Assignee(id=uuid.uuid4(), agent_id=agent_id, harness="")
    assert await _harness_config(tenant_id, assignee, _run_timeout()) == {}


async def test_a_selected_harness_carries_its_connection_and_model(db_available: None) -> None:
    tenant_id, agent_id = await _tenant_with_connection(f"hs-ok-{uuid.uuid4().hex[:8]}")
    assignee = _Assignee(id=uuid.uuid4(), agent_id=agent_id, harness="opencode")

    config = await _harness_config(tenant_id, assignee, _run_timeout())
    assert config["harness"]["key"] == "opencode"
    assert config["harness_agent_id"] == str(agent_id)
    assert config["harness_model"] == "gpt-nano"
    assert config["harness_ttl_seconds"] == _run_timeout()


async def test_a_withheld_harness_degrades_to_codegen_rather_than_failing(
    db_available: None,
) -> None:
    """The internal-policy case, at the moment it bites. A persona configured before the
    harness was withdrawn still names it; the delegation should still produce work, by the
    old path, rather than erroring on a configuration nobody has fixed yet."""
    tenant_id, agent_id = await _tenant_with_connection(f"hs-deny-{uuid.uuid4().hex[:8]}")
    await withhold_harness(tenant_id, "opencode")
    assignee = _Assignee(id=uuid.uuid4(), agent_id=agent_id, harness="opencode")

    assert await _harness_config(tenant_id, assignee, _run_timeout()) == {}


async def test_a_harness_the_tenant_never_registered_is_not_invented(db_available: None) -> None:
    tenant_id, agent_id = await _tenant_with_connection(f"hs-unk-{uuid.uuid4().hex[:8]}")
    assignee = _Assignee(id=uuid.uuid4(), agent_id=agent_id, harness="not-a-harness")

    assert await _harness_config(tenant_id, assignee, _run_timeout()) == {}


async def test_a_tenants_override_is_what_gets_used(db_available: None) -> None:
    """An air-gapped deployment points `opencode` at its own mirror; the persona still
    just says "opencode"."""
    tenant_id, agent_id = await _tenant_with_connection(f"hs-over-{uuid.uuid4().hex[:8]}")
    await register_harness(
        tenant_id,
        "opencode",
        {"image": "mirror.internal/oc:1", "command": "opencode run {prompt_file}"},
    )
    assignee = _Assignee(id=uuid.uuid4(), agent_id=agent_id, harness="opencode")

    config = await _harness_config(tenant_id, assignee, _run_timeout())
    assert config["harness"]["image"] == "mirror.internal/oc:1"


def test_harness_setup_is_appended_to_the_repos_own() -> None:
    """The repo still needs its toolchain -- the harness has to run *that repo's* tests.
    Substituting would leave a container that can run the agent and not the suite."""
    environment: dict[str, Any] = {
        "image": "docker.io/library/node:20-bookworm",
        "setup_cmds": ["apt-get install -y make"],
    }
    _apply_harness(
        environment,
        {"harness": {"key": "oc", "setup_cmds": ["npm i -g opencode-ai@1.18.33"], "image": ""}},
    )
    assert environment["setup_cmds"] == [
        "apt-get install -y make",
        "npm i -g opencode-ai@1.18.33",
    ]
    assert environment["image"] == "docker.io/library/node:20-bookworm", "no image, no override"


def test_a_harness_image_wins_over_the_runtimes() -> None:
    """A pre-baked variant is the whole point of naming an image: it exists so a one-shot
    engine stops paying the install on every run."""
    environment: dict[str, Any] = {"image": "docker.io/library/node:20-bookworm", "setup_cmds": []}
    _apply_harness(environment, {"harness": {"key": "oc", "image": "ghcr.io/x/node20-oc:1"}})
    assert environment["image"] == "ghcr.io/x/node20-oc:1"


def test_an_image_named_for_the_repo_beats_a_harness_image() -> None:
    """The repo's own image was chosen for this code (its toolchain); a harness default
    replacing it would run the tests somewhere they were never meant to run."""
    for source in ("repo", "manifest"):
        environment: dict[str, Any] = {"image": "godot:4", "image_source": source}
        _apply_harness(environment, {"harness": {"key": "oc", "image": "ghcr.io/x/oc:1"}})
        assert environment["image"] == "godot:4", source
    built: dict[str, Any] = {"image": "r/t/godot@sha256:" + "a" * 64, "image_built": True}
    _apply_harness(built, {"harness": {"key": "oc", "image": "ghcr.io/x/oc:1"}})
    assert built["image"].startswith("r/t/godot@")


def test_a_proven_harness_is_not_installed_again() -> None:
    from core.harness.registry import harness_fingerprint

    spec = {"key": "oc", "setup_cmds": ["npm i -g opencode-ai@1.18.33"]}
    environment: dict[str, Any] = {
        "image": "img",
        "image_built": True,
        "setup_cmds": ["make deps"],
        "baked_harness": {"key": "oc", "fingerprint": harness_fingerprint(spec)},
    }
    _apply_harness(environment, {"harness": spec})
    assert environment["setup_cmds"] == ["make deps"]


def test_a_harness_baked_from_other_setup_is_installed_anyway() -> None:
    """An operator bumped the harness version; the image still carries the old one."""
    environment: dict[str, Any] = {
        "image": "img",
        "image_built": True,
        "setup_cmds": [],
        "baked_harness": {"key": "oc", "fingerprint": "stale"},
    }
    _apply_harness(
        environment, {"harness": {"key": "oc", "setup_cmds": ["npm i -g opencode-ai@2"]}}
    )
    assert environment["setup_cmds"] == ["npm i -g opencode-ai@2"]


def test_applying_nothing_changes_nothing() -> None:
    environment: dict[str, Any] = {"image": "i", "setup_cmds": ["a"]}
    _apply_harness(environment, {})
    assert environment == {"image": "i", "setup_cmds": ["a"]}
