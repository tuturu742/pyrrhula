"""Git smart-HTTP for the hosted store — the transport remote exec environments use.

A local container, a k8s Job, or a cloud runner clones and pushes the store repo over
plain https instead of mounting the blobs volume (which only sibling containers on one
host could do). Auth is a short-lived job token scoped to exactly one ``store_key``
(minted per delegation, carried in the URL's Basic-auth password slot:
``https://job:<token>@…``); there is no session/JWT auth here — a git client can't do
that dance.

Protocol: the standard smart-HTTP pair — ``GET info/refs?service=…`` advertises via
``git <service> --stateless-rpc --advertise-refs`` (with the ``# service=`` pkt-line
prologue), ``POST git-upload-pack|git-receive-pack`` pipes the request body through the
same command. Repos are small (delegated work), so bodies are buffered, not streamed.
``main`` stays checked out in the non-bare store repo, so a push to it is refused by
git itself (receive.denyCurrentBranch) — the same guarantee the volume path had.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import Response

from adapters.mcp.git_store import _GIT_ENV, default_git_root
from core.repos.service import verify_artifact_read_token, verify_git_job_token

router = APIRouter(prefix="/git", tags=["git-http"])

_STORE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,80}$")
_SERVICES = ("git-upload-pack", "git-receive-pack")


def _repo_path(store_key: str) -> Path:
    if not _STORE_KEY_RE.fullmatch(store_key):
        raise HTTPException(status_code=404, detail="no such repo")
    path = Path(default_git_root()) / store_key / "repo"
    if not (path / ".git").is_dir():
        raise HTTPException(status_code=404, detail="no such repo")
    return path


def _require_token(request: Request, store_key: str) -> None:
    header = request.headers.get("authorization", "")
    token = ""
    if header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode()
            token = decoded.split(":", 1)[1] if ":" in decoded else decoded
        except Exception:  # noqa: BLE001 -- malformed header == unauthorized
            token = ""
    if not token or not verify_git_job_token(token, store_key):
        # 401 + the Basic challenge makes git prompt/retry correctly.
        raise HTTPException(
            status_code=401,
            detail="git job token required",
            headers={"WWW-Authenticate": 'Basic realm="pyrrhula-git"'},
        )


async def _run_service(service: str, repo: Path, stdin: bytes, *, advertise: bool) -> bytes:
    args = ["git", service.removeprefix("git-"), "--stateless-rpc"]
    if advertise:
        args.append("--advertise-refs")
    args.append(str(repo))
    proc = await asyncio.create_subprocess_exec(
        *args,
        # No pipe when there is no body: git exits fast on --advertise-refs without
        # reading stdin, and communicate(b"") then races the closed transport
        # (RuntimeError: WriteUnixTransport closed -- observed intermittently live).
        stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", **_GIT_ENV},
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin if stdin else None), timeout=120)
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise HTTPException(status_code=500, detail=f"git {service} pipe error") from exc
    if proc.returncode != 0 and not out:
        raise HTTPException(status_code=500, detail=f"git {service} failed: {err[-200:]!r}")
    return out


def _pkt_line(text: str) -> bytes:
    data = text.encode()
    return f"{len(data) + 4:04x}".encode() + data


@router.get("/{store_key}/info/refs")
async def info_refs(store_key: str, request: Request, service: str = Query(...)) -> Response:
    if service not in _SERVICES:
        raise HTTPException(status_code=400, detail="unknown service")
    _require_token(request, store_key)
    repo = _repo_path(store_key)
    advert = await _run_service(service, repo, b"", advertise=True)
    body = _pkt_line(f"# service={service}\n") + b"0000" + advert
    return Response(
        content=body,
        media_type=f"application/x-{service}-advertisement",
        headers={"Cache-Control": "no-cache"},
    )


async def _service_post(store_key: str, request: Request, service: str) -> Response:
    _require_token(request, store_key)
    repo = _repo_path(store_key)
    body = await request.body()
    if request.headers.get("content-encoding") == "gzip":
        body = gzip.decompress(body)
    out = await _run_service(service, repo, body, advertise=False)
    return Response(
        content=out,
        media_type=f"application/x-{service}-result",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/{store_key}/git-upload-pack")
async def upload_pack(store_key: str, request: Request) -> Response:
    return await _service_post(store_key, request, "git-upload-pack")


@router.post("/{store_key}/git-receive-pack")
async def receive_pack(store_key: str, request: Request) -> Response:
    return await _service_post(store_key, request, "git-receive-pack")


# ── QA build artifact intake (M-E) ───────────────────────────────────────────────────
_ARTIFACT_MAX_BYTES = 200 * 1024 * 1024  # a web export zip, not a wagon of debug symbols
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")


@router.post("/{store_key}/artifact", status_code=201)
async def upload_artifact(
    store_key: str, request: Request, name: str = Query(...)
) -> dict[str, str]:
    """A delegation environment POSTs the built artifact here with the same short-lived
    job token it cloned with (curl handles the userinfo-in-URL form). Stored under a
    deterministic blob key -- 'latest' semantics, each green build replaces the last."""
    _require_token(request, store_key)
    _repo_path(store_key)  # 404 before reading the body when the repo doesn't exist
    if not _ARTIFACT_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="invalid artifact name")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty artifact")
    if len(body) > _ARTIFACT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="artifact too large")
    from api.blob_store_factory import get_blob_store

    blob_key = f"artifacts/{store_key}/{name}"
    await get_blob_store().put(blob_key, body)
    return {"blob_key": blob_key, "bytes": str(len(body))}


def _require_artifact_read_token(request: Request, store_key: str, name: str) -> None:
    """Read-only counterpart to ``_require_token``, scoped to one artifact.

    Kept separate on purpose: a push-capable git token must never be what unlocks the
    download a preview container performs."""
    header = request.headers.get("authorization", "")
    token = ""
    if header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode()
            token = decoded.split(":", 1)[1] if ":" in decoded else decoded
        except Exception:  # noqa: BLE001 -- malformed header == unauthorized
            token = ""
    elif header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not token or not verify_artifact_read_token(token, store_key, name):
        raise HTTPException(
            status_code=401,
            detail="artifact read token required",
            headers={"WWW-Authenticate": 'Basic realm="pyrrhula-artifact"'},
        )


@router.get("/{store_key}/artifact")
async def download_artifact(store_key: str, request: Request, name: str = Query(...)) -> Response:
    """A preview container fetches the built artifact here on startup. Same blob key the
    intake writes; a token that can do nothing else."""
    if not _ARTIFACT_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="invalid artifact name")
    _require_artifact_read_token(request, store_key, name)
    _repo_path(store_key)
    from api.blob_store_factory import get_blob_store
    from core.ports.blob_store import BlobNotFoundError

    try:
        body = await get_blob_store().get(f"artifacts/{store_key}/{name}")
    except BlobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="no such artifact") from exc
    return Response(content=body, media_type="application/octet-stream")
