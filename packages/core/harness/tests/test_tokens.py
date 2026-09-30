"""Inference job tokens.

The security property under test is not "a JWT round-trips" but "this credential cannot
be mistaken for, or substituted by, any other credential in the deployment". The git job
token authorizes a push; the artifact token reads a build output; this one spends a
tenant's money. They are all signed with the same deployment secret, so the ``use`` claim
is the only thing keeping them apart.
"""

from __future__ import annotations

import uuid

from core.harness.tokens import (
    InferenceGrant,
    mint_inference_job_token,
    verify_inference_job_token,
)
from core.repos.service import (
    mint_artifact_read_token,
    mint_git_job_token,
    verify_artifact_read_token,
    verify_git_job_token,
)


def _grant() -> InferenceGrant:
    return InferenceGrant(
        tenant_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        persona_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
    )


def _mint(grant: InferenceGrant, ttl: int = 60) -> str:
    return mint_inference_job_token(
        grant.tenant_id, grant.session_id, grant.persona_id, grant.agent_id, ttl_seconds=ttl
    )


def test_every_claim_survives_the_round_trip() -> None:
    """All four are load-bearing downstream: the tenant scopes the session, the connection
    picks model and key, the persona and session are what the usage row is attributed to."""
    grant = _grant()
    assert verify_inference_job_token(_mint(grant)) == grant


def test_rubbish_is_refused() -> None:
    assert verify_inference_job_token("not-a-token") is None
    assert verify_inference_job_token("") is None


def test_an_expired_token_is_refused() -> None:
    assert verify_inference_job_token(_mint(_grant(), ttl=-1)) is None


def test_a_git_token_cannot_buy_inference() -> None:
    """A container already holds a git job token. If that token also authorised model
    calls, the blast radius of leaking it would include the tenant's spend."""
    assert verify_inference_job_token(mint_git_job_token("some-store")) is None
    assert verify_inference_job_token(mint_artifact_read_token("s", "a", ttl_seconds=60)) is None


def test_an_inference_token_cannot_push_or_read_artifacts() -> None:
    """And the converse, which matters more: the inference token is the one handed to a
    process running agent-chosen shell commands."""
    token = _mint(_grant())
    assert verify_git_job_token(token, "some-store") is False
    assert verify_artifact_read_token(token, "some-store", "artifact") is False


def test_a_token_for_one_persona_names_only_that_persona() -> None:
    """The proxy meters against these claims rather than anything in the request body, so
    two personas' tokens must not be interchangeable."""
    first, second = _grant(), _grant()
    verified = verify_inference_job_token(_mint(first))
    assert verified is not None
    assert verified.persona_id == first.persona_id
    assert verified.persona_id != second.persona_id
