"""``ImageBuilder`` over a small, versioned webhook contract (``schema: 1``).

For an operator whose build system is none of the named ones: put a receiver in front of
it that speaks this, and Pyrrhula can use it. The contract (also in
``docs/builders/webhook.md``):

- ``POST <submit_url>`` with JSON ``{schema, build_id, dockerfile_b64, target_ref,
  labels}`` → ``{"external_id": "...", "url": "..."?}``. The Dockerfile is base64 so a
  receiver that pastes it somewhere cannot be talked into running it as shell.
- ``GET <status_url>/<external_id>`` → ``{"state": "queued|building|succeeded|failed|
  cancelled", "digest"?, "log_tail"?, "error"?, "url"?}``.
- ``POST <cancel_url>/<external_id>``, when a cancel URL is configured.

Every request is signed: ``X-Pyrrhula-Timestamp`` (unix seconds) and
``X-Pyrrhula-Signature: sha256=<hex HMAC-SHA256(secret, "<ts>\\n<METHOD>\\n<path>\\n<body>")>``,
so a receiver can refuse anyone who is not this deployment, and a replay older than its
tolerance. An optional bearer token rides ``Authorization`` for receivers behind a gateway.

Hosts are checked by ``core.net_guard`` before every request, https is required unless
the operator marked the builder insecure, and redirects are never followed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from adapters.image_builder.redact import redact
from core.net_guard import BlockedAddressError, check_host
from core.ports.image_builder import (
    BuilderError,
    BuilderProbe,
    BuildSpec,
    Progress,
    Submitted,
)

SCHEMA = 1
_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_STATES = {"queued", "building", "succeeded", "failed", "cancelled"}


def sign(secret: str, timestamp: str, method: str, path: str, body: bytes) -> str:
    message = f"{timestamp}\n{method.upper()}\n{path}\n".encode() + body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify(
    secret: str,
    headers: dict[str, str],
    method: str,
    path: str,
    body: bytes,
    *,
    tolerance: int = 300,
    now: float | None = None,
) -> bool:
    """What a receiver does with a request: the reference implementation, used by tests."""
    timestamp = headers.get("x-pyrrhula-timestamp", "")
    given = headers.get("x-pyrrhula-signature", "")
    if not timestamp.isdigit() or abs((now or time.time()) - int(timestamp)) > tolerance:
        return False
    return hmac.compare_digest(given, sign(secret, timestamp, method, path, body))


@dataclass(frozen=True)
class WebhookConfig:
    submit_url: str
    status_url: str
    cancel_url: str = ""
    insecure: bool = False


@dataclass
class WebhookImageBuilder:
    config: WebhookConfig
    signing_secret: str = field(repr=False)
    token: str = field(default="", repr=False)
    transport: httpx.AsyncBaseTransport | None = None
    guard: bool = True
    mode: Literal["poll", "stream"] = "poll"

    def _secrets(self) -> list[str]:
        return [self.signing_secret, self.token]

    async def _request(self, method: str, url: str, payload: dict[str, Any] | None) -> Any:
        parsed = httpx.URL(url)
        if parsed.scheme != "https" and not (self.config.insecure and parsed.scheme == "http"):
            raise BuilderError("the builder URL must be https (or the builder marked insecure)")
        if self.guard:
            try:
                await check_host(parsed.host)
            except BlockedAddressError as exc:
                raise BuilderError(str(exc)) from exc
            except OSError as exc:
                raise BuilderError(f"cannot resolve {parsed.host}") from exc
        body = json.dumps(payload).encode() if payload is not None else b""
        timestamp = str(int(time.time()))
        path = parsed.raw_path.decode()
        headers = {
            "Content-Type": "application/json",
            "X-Pyrrhula-Timestamp": timestamp,
            "X-Pyrrhula-Signature": sign(self.signing_secret, timestamp, method, path, body),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=20.0, follow_redirects=False
            ) as client:
                resp = await client.request(method, url, content=body or None, headers=headers)
        except httpx.HTTPError as exc:
            raise BuilderError(f"the builder is unreachable ({type(exc).__name__})") from exc
        if resp.status_code >= 300:
            detail = redact(resp.text[:300], self._secrets(), limit=300)
            raise BuilderError(f"the builder answered HTTP {resp.status_code}: {detail}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise BuilderError("the builder answered with something other than JSON") from exc

    def _url(self, value: Any) -> str:
        text = str(value or "")
        return text if text.startswith(("https://", "http://")) and len(text) < 1024 else ""

    async def submit(self, spec: BuildSpec) -> Submitted:
        body = await self._request(
            "POST",
            self.config.submit_url,
            {
                "schema": SCHEMA,
                "build_id": spec.build_id,
                "dockerfile_b64": base64.b64encode(spec.dockerfile.encode()).decode(),
                "target_ref": spec.target_ref,
                "labels": spec.labels,
            },
        )
        external_id = str((body or {}).get("external_id") or "")
        if not _EXTERNAL_ID.match(external_id):
            raise BuilderError("the builder returned no usable external_id")
        return Submitted(external_id, self._url(body.get("url")))

    async def poll(self, external_ref: str) -> Progress:
        if not _EXTERNAL_ID.match(external_ref):
            raise BuilderError("invalid external id")
        body = await self._request(
            "GET", f"{self.config.status_url.rstrip('/')}/{external_ref}", None
        )
        state = str((body or {}).get("state") or "")
        if state not in _STATES:
            raise BuilderError(f"the builder reported an unknown state {state[:40]!r}")
        digest = str(body.get("digest") or "")
        return Progress(
            state,  # type: ignore[arg-type]
            digest=digest if _DIGEST.match(digest) else "",
            log_tail=redact(str(body.get("log_tail") or ""), self._secrets()),
            error=redact(str(body.get("error") or ""), self._secrets(), limit=1000),
            external_url=self._url(body.get("url")),
        )

    async def cancel(self, external_ref: str) -> None:
        if not self.config.cancel_url or not _EXTERNAL_ID.match(external_ref):
            return
        await self._request("POST", f"{self.config.cancel_url.rstrip('/')}/{external_ref}", {})

    async def probe(self) -> BuilderProbe:
        """Reachability and authentication, without building anything: a status request
        for an id that cannot exist. A 404 means the receiver answered and accepted the
        signature; anything else says what went wrong."""
        try:
            await self._request("GET", f"{self.config.status_url.rstrip('/')}/pyrrhula-probe", None)
        except BuilderError as exc:
            if "HTTP 404" in str(exc):
                return BuilderProbe(True, "reachable; signature accepted")
            return BuilderProbe(False, str(exc))
        return BuilderProbe(True, "reachable")
