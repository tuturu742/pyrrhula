"""The webhook builder against a reference receiver: signed, inert, and never trusting."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from adapters.image_builder.redact import redact
from adapters.image_builder.webhook import WebhookConfig, WebhookImageBuilder, verify
from core.ports.image_builder import BuilderError, BuildSpec

SECRET = "whsec-0123456789abcdef"
TOKEN = "tok-SENTINEL-4242"
DIGEST = "sha256:" + "d" * 64
_CONFIG = WebhookConfig(
    submit_url="https://builds.example/submit",
    status_url="https://builds.example/status",
    cancel_url="https://builds.example/cancel",
)


class Receiver:
    """What an operator's receiver is expected to do, including refusing unsigned calls."""

    def __init__(self, state: str = "succeeded", log_tail: str = "") -> None:
        self.state = state
        self.log_tail = log_tail
        self.submitted: list[dict[str, object]] = []
        self.cancelled: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not verify(
            SECRET, headers, request.method, request.url.raw_path.decode(), request.content
        ):
            return httpx.Response(401, text="bad signature")
        path = request.url.path
        if path == "/submit":
            self.submitted.append(json.loads(request.content))
            return httpx.Response(202, json={"external_id": "run-17", "url": "https://ci/run/17"})
        if path.startswith("/status/"):
            ext = path.rsplit("/", 1)[1]
            if ext != "run-17":
                return httpx.Response(404)
            return httpx.Response(
                200, json={"state": self.state, "digest": DIGEST, "log_tail": self.log_tail}
            )
        if path.startswith("/cancel/"):
            self.cancelled.append(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={})
        return httpx.Response(404)


def _builder(
    receiver: object, *, secret: str = SECRET, config: WebhookConfig = _CONFIG
) -> WebhookImageBuilder:
    return WebhookImageBuilder(
        config,
        signing_secret=secret,
        token=TOKEN,
        transport=httpx.MockTransport(receiver),  # type: ignore[arg-type]
        guard=False,
    )


_SPEC = BuildSpec(
    build_id="b-1",
    dockerfile='FROM debian:bookworm\nRUN echo "$(id)"; rm -rf /\n',
    target_ref="reg.example/pyrrhula/tabc/godot:abc-12345678",
)


async def test_a_build_is_submitted_signed_with_the_dockerfile_inert() -> None:
    receiver = Receiver()
    submitted = await _builder(receiver).submit(_SPEC)
    assert submitted.external_ref == "run-17"
    body = receiver.submitted[0]
    assert body["schema"] == 1 and body["target_ref"] == _SPEC.target_ref
    assert "dockerfile" not in body, "never as a raw string a receiver could paste into shell"
    assert base64.b64decode(str(body["dockerfile_b64"])).decode() == _SPEC.dockerfile


async def test_an_unsigned_or_wrongly_signed_call_is_refused_by_the_receiver() -> None:
    with pytest.raises(BuilderError, match="401"):
        await _builder(Receiver(), secret="someone-elses-secret!").submit(_SPEC)


async def test_progress_is_read_and_scrubbed() -> None:
    receiver = Receiver(log_tail=f"step 3: curl -H 'Authorization: Bearer {TOKEN}' x")
    progress = await _builder(receiver).poll("run-17")
    assert progress.state == "succeeded" and progress.digest == DIGEST
    assert TOKEN not in progress.log_tail


async def test_an_unknown_state_is_not_guessed_at() -> None:
    with pytest.raises(BuilderError, match="unknown state"):
        await _builder(Receiver(state="done")).poll("run-17")


async def test_an_external_id_cannot_steer_the_url() -> None:
    with pytest.raises(BuilderError, match="invalid"):
        await _builder(Receiver()).poll("../../admin")


async def test_cancel_reaches_the_receiver() -> None:
    receiver = Receiver()
    await _builder(receiver).cancel("run-17")
    assert receiver.cancelled == ["run-17"]


async def test_redirects_are_not_followed() -> None:
    def moved(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    with pytest.raises(BuilderError, match="302"):
        await _builder(moved).submit(_SPEC)


async def test_plain_http_needs_the_operator_to_say_so() -> None:
    plain = WebhookConfig(submit_url="http://builds/submit", status_url="http://builds/status")
    with pytest.raises(BuilderError, match="https"):
        await _builder(Receiver(), config=plain).submit(_SPEC)


async def test_probe_counts_a_signed_404_as_reachable() -> None:
    probe = await _builder(Receiver()).probe()
    assert probe.ok, probe.detail
    refused = await _builder(Receiver(), secret="wrong-secret-xxxxxx").probe()
    assert not refused.ok and "401" in refused.detail


def test_redaction_catches_the_shapes_that_leak() -> None:
    text = (
        "Authorization: Bearer abcdefghijklmnop\n"
        "https://user:hunter2@registry.example/v2/\n"
        "ghp_" + "A" * 36 + "\n"
        "password=correcthorse\n"
        "and the adapter's own " + TOKEN
    )
    out = redact(text, [TOKEN])
    for leaked in ("abcdefghijklmnop", "hunter2", "ghp_AAAA", "correcthorse", TOKEN):
        assert leaked not in out, leaked
