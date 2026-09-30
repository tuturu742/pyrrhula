"""The inference proxy: a harness's model call, re-issued through our own port.

The point of this route is not that it forwards a request -- anything can forward a
request. It is that a call made from inside a container lands on the same side of
``ModelProvider`` as every other call, so metering, the egress policy and daily caps all
still apply. These tests are about those three, plus the two properties that make the
credential safe to hand to a process running agent-chosen shell commands: it names the
connection itself, and it is not interchangeable with the git token the container already
holds.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from api.main import app
from core.agents.authoring import create_agent, create_persona
from core.audit.models import UsageRecordRow
from core.harness.tokens import mint_inference_job_token
from core.repos.service import mint_git_job_token
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant
from core.usage_limits import set_limits


@pytest.fixture
def client():  # noqa: ANN201
    with TestClient(app) as test_client:
        yield test_client


@pytest_asyncio.fixture
async def harness_grant(db_available: None):  # noqa: ANN201
    """A tenant with an `echo` connection and a persona, plus a token naming both.

    `echo` because these tests are about our bookkeeping, not a vendor's: EchoModelProvider
    answers deterministically and costs nothing, so a failing assertion is always ours.
    """
    tenant_id, _, _ = await seed_dev_tenant(slug=f"inf-{uuid.uuid4().hex[:8]}")
    async with tenant_scope(tenant_id) as session:
        workspace_id = (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()
    agent = await create_agent(
        tenant_id, name="harness", provider="echo", model="echo-1", encryptor=IdentityEncryptor()
    )
    persona = await create_persona(
        tenant_id,
        workspace_id,
        key="dev",
        name="Dev",
        agent_id=agent.id,
        persona_type="participant",
        persona_md="A developer.",
    )
    session_id = uuid.uuid4()
    token = mint_inference_job_token(tenant_id, session_id, persona.id, agent.id, ttl_seconds=300)
    return {
        "tenant_id": tenant_id,
        "workspace_id": workspace_id,
        "agent_id": agent.id,
        "persona_id": persona.id,
        "session_id": session_id,
        "token": token,
    }


def _body(**extra):  # noqa: ANN202, ANN003
    return {"model": "whatever", "messages": [{"role": "user", "content": "hello"}], **extra}


async def _usage_rows(tenant_id: uuid.UUID) -> list[UsageRecordRow]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(UsageRecordRow).where(UsageRecordRow.tenant_id == tenant_id)
            )
        ).scalars()
        return list(rows)


def test_no_token_is_refused(client: TestClient) -> None:
    response = client.post("/inference/v1/chat/completions", json=_body())
    assert response.status_code == 401


def test_the_containers_git_token_cannot_buy_inference(client: TestClient) -> None:
    """The container already holds this one. If it also bought model calls, leaking it
    would put the tenant's spend in the blast radius."""
    git_token = mint_git_job_token("some-store")
    response = client.post(
        "/inference/v1/chat/completions",
        json=_body(),
        headers={"Authorization": f"Bearer {git_token}"},
    )
    assert response.status_code == 401


async def test_a_proxied_call_is_metered_as_delegation(client: TestClient, harness_grant) -> None:  # noqa: ANN001
    """The whole reason this route exists. Today's codegen path writes no usage row at
    all, so delegated spend is invisible to core.usage_limits."""
    before = len(await _usage_rows(harness_grant["tenant_id"]))
    response = client.post(
        "/inference/v1/chat/completions",
        json=_body(),
        headers={"Authorization": f"Bearer {harness_grant['token']}"},
    )
    assert response.status_code == 200, response.text

    rows = await _usage_rows(harness_grant["tenant_id"])
    assert len(rows) == before + 1
    row = rows[-1]
    assert row.purpose == "delegation"
    assert row.persona_id == harness_grant["persona_id"]
    assert row.agent_id == harness_grant["agent_id"]
    assert row.session_id == harness_grant["session_id"]
    assert row.prompt_tokens > 0, "a metered call with zero tokens is not metered"


async def test_the_model_the_harness_asks_for_is_ignored(client: TestClient, harness_grant) -> None:  # noqa: ANN001
    """A harness names a model in its body. The token names the connection, and the
    connection wins -- otherwise a harness could talk its way onto a costlier model."""
    response = client.post(
        "/inference/v1/chat/completions",
        json=_body(model="something-expensive-4-ultra"),
        headers={"Authorization": f"Bearer {harness_grant['token']}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "echo-1"
    rows = await _usage_rows(harness_grant["tenant_id"])
    assert rows[-1].model == "echo-1"
    assert rows[-1].provider == "echo"


async def test_a_daily_cap_stops_a_harness_before_it_spends(
    client: TestClient, harness_grant
) -> None:  # noqa: ANN001
    """An agent loop makes many calls per task, so the cap has to bite per call. 429 so a
    harness can tell 'out of budget' from 'your request was malformed'."""
    await set_limits(harness_grant["tenant_id"], {"tenant_daily_tokens": 1})
    client.post(
        "/inference/v1/chat/completions",
        json=_body(),
        headers={"Authorization": f"Bearer {harness_grant['token']}"},
    )
    response = client.post(
        "/inference/v1/chat/completions",
        json=_body(),
        headers={"Authorization": f"Bearer {harness_grant['token']}"},
    )
    assert response.status_code == 429, response.text


async def test_streaming_answers_in_the_dialect_a_harness_expects(
    client: TestClient, harness_grant
) -> None:  # noqa: ANN001
    """opencode streams. The frames have to be OpenAI chat.completion.chunk SSE, ending
    with [DONE], or the harness hangs waiting for an end it never sees."""
    response = client.post(
        "/inference/v1/chat/completions",
        json=_body(stream=True),
        headers={"Authorization": f"Bearer {harness_grant['token']}"},
    )
    assert response.status_code == 200, response.text
    body = response.text
    assert "chat.completion.chunk" in body
    assert body.rstrip().endswith("data: [DONE]")
    assert len(await _usage_rows(harness_grant["tenant_id"])) == 1, "a stream must meter too"


async def test_the_tenants_egress_policy_still_applies(client: TestClient, harness_grant) -> None:  # noqa: ANN001
    """Rule 11 puts the egress check inside the ModelProvider port, keyed on `purpose`. A
    harness calling a vendor directly would never cross that port; calling through here it
    does, so a tenant that confines delegated work to local models is obeyed."""
    from sqlalchemy.orm.attributes import flag_modified

    from core.tenancy.egress import invalidate_egress_policy
    from core.tenancy.models import Tenant

    async with tenant_scope(harness_grant["tenant_id"]) as session:
        tenant = await session.get(Tenant, harness_grant["tenant_id"])
        assert tenant is not None
        settings = dict(tenant.settings or {})
        settings["egress_policy"] = {"delegation": ["local"]}
        tenant.settings = settings
        flag_modified(tenant, "settings")
    invalidate_egress_policy(harness_grant["tenant_id"])

    response = client.post(
        "/inference/v1/chat/completions",
        json=_body(),
        headers={"Authorization": f"Bearer {harness_grant['token']}"},
    )
    assert response.status_code == 403, response.text
    assert not await _usage_rows(harness_grant["tenant_id"]), "a refused call must not meter"


async def test_the_model_listing_offers_only_what_the_token_allows(
    client: TestClient, harness_grant
) -> None:  # noqa: ANN001
    """Harnesses list models to validate config. Offering the provider's whole catalogue
    would invite a choice this route then ignores."""
    response = client.get(
        "/inference/v1/models", headers={"Authorization": f"Bearer {harness_grant['token']}"}
    )
    assert response.status_code == 200, response.text
    assert [m["id"] for m in response.json()["data"]] == ["echo-1"]
