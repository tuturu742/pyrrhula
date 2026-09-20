"""C1.6 acceptance criteria for the ToolDefinition registry, against a live Postgres."""

from __future__ import annotations

import uuid

import pydantic
import pytest

from core.resolution.registry import (
    RANDOMIZER_DEFINITION,
    ToolDefinitionSchema,
    get_tool_definition,
    register_tool_definition,
)
from core.tenancy.seed import seed_dev_tenant


async def test_register_and_get_tool_definition_round_trips(db_available: None) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"tooldef-{uuid.uuid4().hex[:8]}"
    )
    row = await register_tool_definition(tenant_id, RANDOMIZER_DEFINITION)
    assert row.key == "randomizer"
    assert row.kind == "deterministic"
    assert row.determinism == "seeded_random"

    fetched = await get_tool_definition(tenant_id, "randomizer")
    assert fetched is not None
    assert fetched.id == row.id


async def test_register_tool_definition_is_an_upsert_by_key(db_available: None) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"tooldef-upsert-{uuid.uuid4().hex[:8]}"
    )
    first = await register_tool_definition(tenant_id, RANDOMIZER_DEFINITION)
    updated = RANDOMIZER_DEFINITION.model_copy(update={"validation_ref": "mvp_d20"})
    second = await register_tool_definition(tenant_id, updated)

    assert first.id == second.id
    assert second.validation_ref == "mvp_d20"


def test_definition_schema_forbids_unknown_fields() -> None:
    with pytest.raises(pydantic.ValidationError):
        ToolDefinitionSchema(
            key="x",
            kind="deterministic",
            input_schema={},
            output_schema={},
            impl_ref="builtin:x",
            determinism="pure",
            not_a_real_field="oops",  # type: ignore[call-arg]
        )
