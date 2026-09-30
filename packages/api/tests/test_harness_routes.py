"""The harness catalog over real HTTP, and the one thing a persona may do with it.

Two halves, because they sit at different trust levels on purpose. Registering a harness
writes a shell command that runs in an execution environment, so it needs
``manage_tenant``. *Selecting* one is an ordinary workspace edit -- but only from what is
on offer, which is what makes "a persona may not introduce a harness" a property of the
system rather than a convention.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text

from api.main import app
from api.redis_client import get_redis
from core.tenancy.scope import tenant_scope, unscoped_session
from core.tenancy.seed import seed_dev_tenant


@pytest_asyncio.fixture(autouse=True)
async def _reset_ip_rate_limit(redis_available: None) -> None:
    await get_redis().delete("ratelimit:ip:testclient")


@pytest.fixture
def client():  # noqa: ANN201
    with TestClient(app) as test_client:
        yield test_client


async def _promote(slug: str, email: str, role: str) -> None:
    async with unscoped_session() as session:
        tid = (
            await session.execute(text("select id from tenant where slug=:s"), {"s": slug})
        ).scalar_one()
    async with tenant_scope(tid) as session:
        await session.execute(
            text(
                "update membership set role=:r where principal_id = "
                "(select principal_id from identity where provider='local' and external_id=:e)"
            ),
            {"r": role, "e": email},
        )


def _register(client: TestClient, slug: str) -> tuple[dict[str, str], str]:
    email = f"u-{uuid.uuid4().hex[:8]}@example.com"
    response = client.post(
        "/auth/register",
        json={"email": email, "password": "correct horse battery", "display_name": "U"},
        headers={"X-Pyrrhula-Tenant": slug},
    )
    assert response.status_code == 200, response.text
    return {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "X-Pyrrhula-Tenant": slug,
    }, email


_SPEC = {
    "command": "acme-agent --task {prompt_file} --model {model}",
    "image": "registry.acme.internal/acme-agent:2026.9",
    "env": {"ACME_BASE_URL": "{inference_base_url}", "ACME_KEY": "{inference_token}"},
    "licence": "proprietary",
    "redistributable": False,
}


async def test_the_builtin_is_offered_and_can_be_withheld_and_restored(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    """The full arc of "internal policy forbids opencode here", and of changing your mind.

    A built-in lives in code, so withholding is a registration that says no rather than a
    delete -- and removing that registration is what brings the built-in back."""
    slug = f"harness-mask-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    headers, email = _register(client, slug)
    await _promote(slug, email, "owner")

    listing = client.get("/harnesses", headers=headers)
    assert listing.status_code == 200, listing.text
    assert "opencode" in {h["key"] for h in listing.json()}
    assert all(h["tenant_owned"] is False for h in listing.json())

    assert client.post("/harnesses/opencode/withhold", headers=headers).status_code == 204
    assert "opencode" not in {h["key"] for h in client.get("/harnesses", headers=headers).json()}

    assert client.delete("/harnesses/opencode", headers=headers).status_code == 204
    assert "opencode" in {h["key"] for h in client.get("/harnesses", headers=headers).json()}


async def test_a_custom_harness_needs_no_code_from_us(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    """The enterprise case: a harness this project has never heard of, registered as data
    and offered to personas immediately."""
    slug = f"harness-custom-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    headers, email = _register(client, slug)
    await _promote(slug, email, "owner")

    resp = client.put("/harnesses/acme-agent", json=_SPEC, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_owned"] is True
    assert resp.json()["licence"] == "proprietary"

    offered = {h["key"]: h for h in client.get("/harnesses", headers=headers).json()}
    assert offered["acme-agent"]["image"] == _SPEC["image"]
    assert offered["acme-agent"]["tenant_owned"] is True


async def test_a_spec_whose_template_we_cannot_fill_is_refused_at_registration(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    """Not left to fail in a container: a typo in a placeholder is otherwise a delegation
    that runs a command with a literal ``{promptfile}`` in it."""
    slug = f"harness-typo-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    headers, email = _register(client, slug)
    await _promote(slug, email, "owner")

    resp = client.put(
        "/harnesses/acme-agent",
        json={**_SPEC, "command": "acme-agent --task {prompt_file} --repo {checkout_dir}"},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    assert "checkout_dir" in resp.text


async def test_a_member_may_read_the_catalog_but_not_write_it(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    """A harness command is operator-supplied shell at the same trust level as
    ``setup_cmds``. An ordinary member picking personas is not that."""
    slug = f"harness-authz-{uuid.uuid4().hex[:8]}"
    await seed_dev_tenant(slug=slug)
    headers, _email = _register(client, slug)  # left at whatever register grants

    assert client.get("/harnesses", headers=headers).status_code == 200
    assert client.put("/harnesses/acme-agent", json=_SPEC, headers=headers).status_code == 403
    assert client.post("/harnesses/opencode/withhold", headers=headers).status_code == 403
    assert client.delete("/harnesses/opencode", headers=headers).status_code == 403


def _profile(client: TestClient, headers: dict[str, str]) -> str:
    resp = client.post(
        "/model-profiles",
        json={"name": "Echo", "provider": "echo", "model": "echo-1"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def test_a_persona_may_select_an_offered_harness_and_only_an_offered_one(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    """The selection guard. Choosing a harness the tenant does not have is a mistake worth
    saying out loud to the person making it, now -- as against a harness *withdrawn* after
    the fact, which is not that persona's mistake and degrades to codegen at run time."""
    slug = f"harness-select-{uuid.uuid4().hex[:8]}"
    _tid, _owner, workspace_id = await seed_dev_tenant(slug=slug)
    headers, email = _register(client, slug)
    await _promote(slug, email, "owner")
    profile_id = _profile(client, headers)

    def make(key: str, harness: str):  # noqa: ANN202
        return client.post(
            "/agents",
            json={
                "workspace_id": str(workspace_id),
                "key": key,
                "name": key,
                "agent_id": profile_id,
                "harness": harness,
            },
            headers=headers,
        )

    good = make("dev", "opencode")
    assert good.status_code == 201, good.text
    assert good.json()["harness"] == "opencode"

    bad = make("dev-b", "claude-code")
    assert bad.status_code == 422, bad.text
    assert "claude-code" in bad.text

    # And the default is off: nothing about an existing persona changes by our shipping a
    # harness at all.
    plain = make("dev-c", "")
    assert plain.status_code == 201, plain.text
    assert plain.json()["harness"] == ""


async def test_selecting_a_withheld_harness_is_refused(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    """The two halves meeting: an admin's "not here" is what a persona editor sees."""
    slug = f"harness-withheld-{uuid.uuid4().hex[:8]}"
    _tid, _owner, workspace_id = await seed_dev_tenant(slug=slug)
    headers, email = _register(client, slug)
    await _promote(slug, email, "owner")
    profile_id = _profile(client, headers)

    assert client.post("/harnesses/opencode/withhold", headers=headers).status_code == 204
    resp = client.post(
        "/agents",
        json={
            "workspace_id": str(workspace_id),
            "key": "dev",
            "name": "Dev",
            "agent_id": profile_id,
            "harness": "opencode",
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


async def test_a_persona_can_be_moved_onto_and_off_a_harness(
    client: TestClient, db_available: None, redis_available: None
) -> None:
    slug = f"harness-patch-{uuid.uuid4().hex[:8]}"
    _tid, _owner, workspace_id = await seed_dev_tenant(slug=slug)
    headers, email = _register(client, slug)
    await _promote(slug, email, "owner")
    profile_id = _profile(client, headers)

    created = client.post(
        "/agents",
        json={
            "workspace_id": str(workspace_id),
            "key": "dev",
            "name": "Dev",
            "agent_id": profile_id,
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    persona_id = created.json()["id"]

    on = client.patch(f"/agents/{persona_id}", json={"harness": "opencode"}, headers=headers)
    assert on.status_code == 200, on.text
    assert on.json()["harness"] == "opencode"

    # An omitted field leaves it alone; an explicit "" turns it off.
    untouched = client.patch(f"/agents/{persona_id}", json={"name": "Dev II"}, headers=headers)
    assert untouched.json()["harness"] == "opencode"
    off = client.patch(f"/agents/{persona_id}", json={"harness": ""}, headers=headers)
    assert off.json()["harness"] == ""
