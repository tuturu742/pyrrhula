"""Preview share tokens and the serving command.

The token is the whole authorization story for a public link, and the command is the
whole lifetime of the container -- neither gets a second chance at runtime, so both are
pinned here.
"""

from __future__ import annotations

import base64
import re
import uuid

import pytest

from core.previews.service import _SERVE_PROGRAM, _WEBROOT, build_serve_command, preview_name
from core.previews.tokens import mint_preview_token, read_preview_token
from core.repos.service import (
    mint_artifact_read_token,
    mint_git_job_token,
    verify_artifact_read_token,
    verify_git_job_token,
)


def test_preview_token_round_trips_tenant_and_preview() -> None:
    tenant_id, preview_id = uuid.uuid4(), uuid.uuid4()
    token = mint_preview_token(tenant_id, preview_id, ttl_seconds=60)
    assert read_preview_token(token) == (tenant_id, preview_id)


def test_expired_preview_token_is_rejected() -> None:
    token = mint_preview_token(uuid.uuid4(), uuid.uuid4(), ttl_seconds=-1)
    assert read_preview_token(token) is None


def test_garbage_token_is_rejected_rather_than_raising() -> None:
    # The public route turns None into a 404; a raise would leak that the token parsed.
    assert read_preview_token("not-a-jwt") is None


def test_preview_token_is_not_accepted_as_a_git_credential() -> None:
    """The preview container is reachable through an anonymous link. If its token could
    stand in for a git job token it would carry push rights to the repo."""
    token = mint_preview_token(uuid.uuid4(), uuid.uuid4(), ttl_seconds=60)
    assert verify_git_job_token(token, "t1234567-vgame") is False
    assert verify_artifact_read_token(token, "t1234567-vgame", "game-web.tar.gz") is False


def test_artifact_and_git_tokens_do_not_cross_over() -> None:
    store, name = "t1234567-vgame", "game-web.tar.gz"
    artifact = mint_artifact_read_token(store, name, ttl_seconds=60)
    git = mint_git_job_token(store)

    assert verify_artifact_read_token(artifact, store, name) is True
    # A read-only artifact token must not unlock git (i.e. push).
    assert verify_git_job_token(artifact, store) is False
    # And a push-capable token is not what the preview container gets to use.
    assert verify_artifact_read_token(git, store, name) is False


def test_artifact_token_is_scoped_to_one_artifact_and_repo() -> None:
    token = mint_artifact_read_token("t1234567-vgame", "game-web.tar.gz", ttl_seconds=60)
    assert verify_artifact_read_token(token, "t1234567-vgame", "other.tar.gz") is False
    assert verify_artifact_read_token(token, "t9999999-other", "game-web.tar.gz") is False


def test_preview_name_is_deterministic_and_a_valid_dns_label() -> None:
    """The name is both the idempotency key and, on socket engines, the container's DNS
    alias -- so it has to be stable and hostname-legal."""
    repo_id = uuid.uuid4()
    assert preview_name(repo_id) == preview_name(repo_id)
    assert re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", preview_name(repo_id))
    assert len(preview_name(repo_id)) <= 63
    assert preview_name(repo_id) != preview_name(uuid.uuid4())


def test_serve_program_is_valid_python_after_substitution() -> None:
    program = _SERVE_PROGRAM.replace("__WEBROOT__", _WEBROOT).replace("__PORT__", "8080")
    compile(program, "serve.py", "exec")  # raises if the substitution broke the source
    assert "__PORT__" not in program and "__WEBROOT__" not in program


def test_serve_command_carries_the_program_intact() -> None:
    command = build_serve_command(port=8080)
    encoded = re.search(r"b64decode\('([^']+)'\)", command)
    assert encoded is not None
    decoded = base64.b64decode(encoded.group(1)).decode()
    compile(decoded, "serve.py", "exec")
    assert 'ThreadingHTTPServer(("0.0.0.0", 8080)' in decoded


def test_serve_command_needs_no_shell_quoting() -> None:
    """It is interpolated into ``sh -lc``; a quote or backtick in the payload would break
    the container's entrypoint in a way only visible at deploy time."""
    command = build_serve_command(port=8080)
    payload = re.search(r"b64decode\('([^']+)'\)", command)
    assert payload is not None
    assert re.fullmatch(r"[A-Za-z0-9+/=]+", payload.group(1))


def test_serve_command_does_not_embed_the_token() -> None:
    """The credential must arrive in the environment: the command is visible in `ps`, in
    the pod spec, and in the ECS task definition."""
    command = build_serve_command(port=8080)
    assert (
        "PYR_ARTIFACT_TOKEN"
        in base64.b64decode(
            re.search(r"b64decode\('([^']+)'\)", command).group(1)  # type: ignore[union-attr]
        ).decode()
    )


@pytest.mark.parametrize("port", [8080, 3000])
def test_serve_program_binds_the_requested_port(port: int) -> None:
    decoded = base64.b64decode(
        re.search(r"b64decode\('([^']+)'\)", build_serve_command(port=port)).group(1)  # type: ignore[union-attr]
    ).decode()
    assert f'("0.0.0.0", {port})' in decoded
