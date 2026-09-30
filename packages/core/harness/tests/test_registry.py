"""The harness registry.

Three properties carry the design, and each of them came from a question worth taking
seriously: can a tenant be *stopped* from using a harness, can a tenant add one we have
never heard of without our code, and can a spec smuggle in execution we did not intend.
"""

from __future__ import annotations

import uuid

import pytest

from core.harness.registry import (
    BUILTIN_HARNESSES,
    PLACEHOLDERS,
    InvalidHarnessError,
    get_harness,
    register_harness,
    remove_harness,
    render,
    resolved_harnesses,
    validate_spec,
    withhold_harness,
)
from core.tenancy.seed import seed_dev_tenant

ENTERPRISE = {
    "image": "registry.acme.internal/claude-code-enterprise:2026.9",
    "command": "acme-agent --task {prompt_file} --model {model}",
    "env": {"ACME_BASE_URL": "{inference_base_url}", "ACME_KEY": "{inference_token}"},
    "wire_format": "openai",
    "licence": "proprietary",
    "redistributable": False,
}


def test_the_builtin_is_a_valid_spec_by_its_own_rules() -> None:
    """A built-in that would be refused if a tenant submitted it is a trap."""
    for key, spec in BUILTIN_HARNESSES.items():
        assert validate_spec(key, spec)


def test_a_command_must_be_told_the_task() -> None:
    """Without {prompt_file} the harness runs against whatever it finds in the tree."""
    with pytest.raises(InvalidHarnessError, match="prompt_file"):
        validate_spec("x", {"command": "opencode run --auto"})


def test_an_unknown_placeholder_is_refused_at_registration() -> None:
    """Not at run time. Otherwise a typo becomes a container that runs a command with a
    literal '{promptfile}' in it and reports a baffling exit code."""
    with pytest.raises(InvalidHarnessError, match="unknown placeholder"):
        validate_spec("x", {"command": "run {prompt_file} --key {api_key}"})


def test_an_unserved_wire_format_is_refused_at_registration() -> None:
    """The dialect has to match a proxy route we actually serve."""
    with pytest.raises(InvalidHarnessError, match="wire_format"):
        validate_spec("x", {"command": "run {prompt_file}", "wire_format": "anthropic"})


def test_the_harness_own_env_indirection_is_left_alone() -> None:
    """`{env:VAR}` is opencode's own substitution, resolved inside the container -- it is
    how a key reaches a config file without being written into it, so validation must not
    mistake it for one of ours."""
    spec = validate_spec(
        "x",
        {
            "command": "run {prompt_file}",
            "config_files": {"/c.json": '{"apiKey":"{env:PYR_INFERENCE_KEY}"}'},
        },
    )
    assert "{env:PYR_INFERENCE_KEY}" in spec["config_files"]["/c.json"]


def test_render_substitutes_only_the_whitelist() -> None:
    out = render(
        "run {prompt_file} --model {model} --url {inference_base_url} {env:KEEP} {unknown}",
        {"prompt_file": "/t.md", "model": "m", "inference_base_url": "http://api"},
    )
    assert "/t.md" in out and "--model m" in out and "http://api" in out
    # Anything not supplied is left verbatim rather than blanked: a silently empty
    # argument is harder to diagnose than one that still reads as a placeholder.
    assert "{env:KEEP}" in out and "{unknown}" in out


def test_render_does_not_evaluate_anything() -> None:
    """str.format would honour attribute access and indexing on whatever it was handed.
    The values are not operator-written, so the substitution must be dumb."""
    out = render("{prompt_file}", {"prompt_file": "{model}"})
    assert out == "{model}", "a substituted value must not itself be substituted"


async def test_builtins_are_the_floor_and_a_tenant_adds_its_own(db_available: None) -> None:
    tenant_id, _, _ = await seed_dev_tenant(slug=f"h-add-{uuid.uuid4().hex[:8]}")
    assert "opencode" in await resolved_harnesses(tenant_id)

    await register_harness(tenant_id, "acme", ENTERPRISE)
    resolved = await resolved_harnesses(tenant_id)
    assert set(resolved) >= {"opencode", "acme"}
    assert resolved["acme"]["image"].startswith("registry.acme.internal/")
    assert resolved["acme"]["redistributable"] is False


async def test_a_tenant_can_be_withheld_a_builtin(db_available: None) -> None:
    """The 'internal policy forbids opencode' case. A built-in lives in code, so a tenant
    cannot delete one -- masking is a registration that says no."""
    tenant_id, _, _ = await seed_dev_tenant(slug=f"h-deny-{uuid.uuid4().hex[:8]}")
    await withhold_harness(tenant_id, "opencode")

    assert "opencode" not in await resolved_harnesses(tenant_id)
    assert await get_harness(tenant_id, "opencode") is None


async def test_withholding_is_reversible(db_available: None) -> None:
    tenant_id, _, _ = await seed_dev_tenant(slug=f"h-undo-{uuid.uuid4().hex[:8]}")
    await withhold_harness(tenant_id, "opencode")
    assert await get_harness(tenant_id, "opencode") is None

    assert await remove_harness(tenant_id, "opencode") is True
    assert await get_harness(tenant_id, "opencode") is not None


async def test_a_tenants_entry_overrides_a_builtin_of_the_same_name(db_available: None) -> None:
    """An air-gapped deployment points `opencode` at its internal mirror once, and every
    persona that says `opencode` follows."""
    tenant_id, _, _ = await seed_dev_tenant(slug=f"h-over-{uuid.uuid4().hex[:8]}")
    await register_harness(
        tenant_id,
        "opencode",
        {"image": "mirror.internal/opencode:1.18.33", "command": "opencode run {prompt_file}"},
    )
    harness = await get_harness(tenant_id, "opencode")
    assert harness is not None
    assert harness["image"] == "mirror.internal/opencode:1.18.33"


async def test_one_tenants_harness_is_not_anothers(db_available: None) -> None:
    suffix = uuid.uuid4().hex[:8]
    first, _, _ = await seed_dev_tenant(slug=f"h-a-{suffix}")
    second, _, _ = await seed_dev_tenant(slug=f"h-b-{suffix}")
    await register_harness(first, "acme", ENTERPRISE)

    assert await get_harness(first, "acme") is not None
    assert await get_harness(second, "acme") is None


async def test_selecting_nothing_resolves_to_nothing(db_available: None) -> None:
    """The default. A persona with no harness keeps the existing codegen path."""
    tenant_id, _, _ = await seed_dev_tenant(slug=f"h-none-{uuid.uuid4().hex[:8]}")
    assert await get_harness(tenant_id, None) is None
    assert await get_harness(tenant_id, "") is None


def test_every_placeholder_the_builtin_uses_is_one_we_supply() -> None:
    """Belt and braces on the contract between this module and the script builder: if the
    built-in referenced something the script step does not pass, every opencode delegation
    would run with a literal placeholder in its command."""
    import re

    text = " ".join(
        [BUILTIN_HARNESSES["opencode"]["command"]]
        + list(BUILTIN_HARNESSES["opencode"]["env"].values())
        + list(BUILTIN_HARNESSES["opencode"]["config_files"].values())
    )
    used = {name for name in re.findall(r"\{([a-z_]+)\}", text) if not name.startswith("env")}
    assert used <= PLACEHOLDERS, (
        f"built-in uses placeholders we do not supply: {used - PLACEHOLDERS}"
    )
