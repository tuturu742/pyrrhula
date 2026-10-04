"""A reference receiver for the webhook builder contract (docs/builders/webhook.md).

Standard library only, so it can be read in one sitting and copied. It builds with a local
``docker`` (or ``podman``) CLI and pushes to whatever registry ``target_ref`` names, using
the credentials that CLI is already logged in with. Run it on the machine you want
tenants' RUN steps to execute on -- not on a Pyrrhula host.

    PYR_SIGNING_SECRET=… PYR_BUILD_CLI=podman python webhook_receiver.py --port 8765

It is a starting point, not a hardened service: one build at a time, state in memory.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ.get("PYR_SIGNING_SECRET", "")
CLI = os.environ.get("PYR_BUILD_CLI", "docker")
# Optional: e.g. "--tls-verify=false" for a plain-HTTP test registry with podman.
PUSH_FLAGS = os.environ.get("PYR_PUSH_FLAGS", "").split()
# Builds get no network beyond what they need to fetch packages; tighten to taste.
BUILD_FLAGS = os.environ.get("PYR_BUILD_FLAGS", "--pull").split()

_REF = re.compile(r"^[a-z0-9.:/_-]+:[A-Za-z0-9._-]+$")
_BUILDS: dict[str, dict[str, object]] = {}
_LOCK = threading.Lock()


def _verify(headers: dict[str, str], method: str, path: str, body: bytes) -> bool:
    ts = headers.get("x-pyrrhula-timestamp", "")
    if not SECRET or not ts.isdigit() or abs(time.time() - int(ts)) > 300:
        return False
    message = f"{ts}\n{method}\n{path}\n".encode() + body
    expected = "sha256=" + hmac.new(SECRET.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(headers.get("x-pyrrhula-signature", ""), expected)


def _run(external_id: str, dockerfile: str, target: str) -> None:
    def update(**values: object) -> None:
        with _LOCK:
            _BUILDS[external_id].update(values)

    update(state="building")
    with tempfile.TemporaryDirectory() as context:  # empty: the build sees no files
        path = os.path.join(context, "Dockerfile")
        with open(path, "w") as fh:
            fh.write(dockerfile)
        # The Dockerfile is a FILE argument -- never interpolated into a command.
        build = subprocess.run(
            [CLI, "build", *BUILD_FLAGS, "-f", path, "-t", target, context],
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            update(
                state="failed", error="build failed", log_tail=(build.stdout + build.stderr)[-4000:]
            )
            return
        with tempfile.NamedTemporaryFile("r", suffix=".digest") as digest_file:
            push = subprocess.run(
                [
                    CLI,
                    "push",
                    *PUSH_FLAGS,
                    *(["--digestfile", digest_file.name] if CLI == "podman" else []),
                    target,
                ],
                capture_output=True,
                text=True,
            )
            digest = digest_file.read().strip() if CLI == "podman" else ""
        if push.returncode != 0:
            update(
                state="failed", error="push failed", log_tail=(push.stdout + push.stderr)[-4000:]
            )
            return
        update(state="succeeded", digest=digest, log_tail=build.stdout[-4000:])


class Handler(BaseHTTPRequestHandler):
    def _answer(self, code: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _checked(self, body: bytes) -> bool:
        headers = {k.lower(): v for k, v in self.headers.items()}
        if _verify(headers, self.command, self.path, body):
            return True
        self._answer(401, {"error": "bad signature"})
        return False

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not self._checked(body):
            return
        if self.path == "/submit":
            request = json.loads(body)
            target = str(request.get("target_ref", ""))
            if request.get("schema") != 1 or not _REF.match(target):
                self._answer(422, {"error": "bad request"})
                return
            dockerfile = base64.b64decode(request["dockerfile_b64"]).decode()
            external_id = uuid.uuid4().hex
            with _LOCK:
                _BUILDS[external_id] = {"state": "queued"}
            threading.Thread(
                target=_run, args=(external_id, dockerfile, target), daemon=True
            ).start()
            self._answer(202, {"external_id": external_id})
            return
        self._answer(404, {})

    def do_GET(self) -> None:  # noqa: N802
        if not self._checked(b""):
            return
        if self.path.startswith("/status/"):
            with _LOCK:
                build = _BUILDS.get(self.path.rsplit("/", 1)[1])
            if build is None:
                self._answer(404, {})
            else:
                self._answer(200, build)
            return
        self._answer(404, {})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if len(SECRET) < 16:
        raise SystemExit(
            "set PYR_SIGNING_SECRET (16+ characters), the same one declared in Pyrrhula"
        )
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
