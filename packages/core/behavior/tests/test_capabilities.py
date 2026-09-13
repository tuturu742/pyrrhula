"""E2.9's core acceptance criteria: a high-stakes axis is rejected on a model the
capability matrix marks incapable, and mutating the stored capability result flips that
outcome without any code change.
"""

from __future__ import annotations

import uuid

import pytest

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent, create_persona
from core.behavior.capabilities import upsert_capability
from core.behavior.capability_validation import (
    CapabilityRejectedError,
    validate_profile_capability,
)
from core.behavior.fixtures import RPG_AXIS_PACK_ID, SECRET_DISCLOSURE_PROPENSITY, TALKATIVENESS
from core.behavior.repo import create_axis_definition
from core.behavior.validation import AxisDefinitionSchema
from core.tenancy.seed import seed_dev_tenant


async def _setup(slug_prefix: str, *, provider: str, model: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    await create_axis_definition(
        tenant_id, AxisDefinitionSchema.model_validate(SECRET_DISCLOSURE_PROPENSITY)
    )
    agent = await create_agent(
        tenant_id, "cap-test-profile", provider, model, encryptor=IdentityEncryptor()
    )
    agent = await create_persona(
        tenant_id,
        workspace_id,
        key="cap-test-agent",
        name="Cap Test Persona",
        agent_id=agent.id,
    )
    return tenant_id, agent.id


async def test_high_stakes_axis_rejected_on_incapable_model(db_available: None) -> None:
    provider, model = "echo", "echo-incapable-1"
    tenant_id, persona_id = await _setup("cap-rejected", provider=provider, model=model)
    await upsert_capability(
        provider,
        model,
        "secret_disclosure_propensity",
        capable=False,
        reason="structured-output fidelity below threshold",
    )

    with pytest.raises(CapabilityRejectedError, match=model) as exc_info:
        await validate_profile_capability(
            tenant_id,
            persona_id,
            RPG_AXIS_PACK_ID,
            {"secret_disclosure_propensity": 80},
        )
    assert "secret_disclosure_propensity" in str(exc_info.value)


async def test_capabilities_reflect_latest_eval_results(db_available: None) -> None:
    provider, model = "echo", "echo-flip-1"
    tenant_id, persona_id = await _setup("cap-flip", provider=provider, model=model)
    axis_values = {"secret_disclosure_propensity": 80}

    await upsert_capability(provider, model, "secret_disclosure_propensity", capable=True)
    await validate_profile_capability(tenant_id, persona_id, RPG_AXIS_PACK_ID, axis_values)

    # Mutating the stored eval result -- no code change -- flips the outcome.
    await upsert_capability(
        provider,
        model,
        "secret_disclosure_propensity",
        capable=False,
        reason="behavioral fidelity regressed in the latest nightly run",
    )
    with pytest.raises(CapabilityRejectedError):
        await validate_profile_capability(tenant_id, persona_id, RPG_AXIS_PACK_ID, axis_values)


async def test_low_stakes_axis_is_never_rejected(db_available: None) -> None:
    """Only stakes:high axes are enforced -- a low-stakes axis degrading on an
    incapable model is a quality signal (E2.8's behavioral_fidelity), not a blocker."""
    provider, model = "echo", "echo-lowstakes-1"
    tenant_id, persona_id = await _setup("cap-lowstakes", provider=provider, model=model)
    await create_axis_definition(tenant_id, AxisDefinitionSchema.model_validate(TALKATIVENESS))
    await upsert_capability(provider, model, "talkativeness", capable=False, reason="whatever")

    await validate_profile_capability(
        tenant_id, persona_id, RPG_AXIS_PACK_ID, {"talkativeness": 50}
    )
