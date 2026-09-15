"""Preview environment lifecycle: rows, the serving command, and expiry.

The engine adapters know how to start a container; this module decides *what* it runs and
keeps the tenant-scoped record of it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from core.previews.models import ACTIVE_STATUSES, PreviewEnvironmentRow
from core.tenancy.scope import tenant_scope

# The container downloads to here and serves it; nothing else in the image is exposed.
_WEBROOT = "/srv/preview"

# Dependency-free on purpose. python:3.12-slim ships neither curl nor wget (the
# delegation script does a curl-or-wget dance for exactly this reason), so the whole
# fetch-extract-serve is one stdlib program. The token arrives in the environment rather
# than in the command string. Placeholders are __SENTINELS__ rather than str.format
# fields because the program itself contains braces.
_SERVE_PROGRAM = """
import base64, io, os, sys, tarfile, urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

url, token = os.environ["PYR_ARTIFACT_URL"], os.environ["PYR_ARTIFACT_TOKEN"]
root = "__WEBROOT__"
os.makedirs(root, exist_ok=True)

auth = base64.b64encode(("job:" + token).encode()).decode()
req = urllib.request.Request(url, headers={"Authorization": "Basic " + auth})
with urllib.request.urlopen(req, timeout=120) as resp:
    payload = resp.read()
print("artifact: %d bytes" % len(payload), flush=True)

with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
    # filter="data" refuses absolute paths, traversal and special files (PEP 706) --
    # the same guard the api-side play route applies to this same tarball.
    tar.extractall(root, filter="data")

os.chdir(root)

serve_cmd = __SERVE_CMD__
if serve_cmd:
    # A recipe took over: the platform's job ended when the artifact landed here safely.
    # exec (not spawn) so the container's lifetime is the served process's lifetime and
    # the engine's restart/teardown semantics keep working unchanged.
    print("serving via recipe", flush=True)
    os.execv("/bin/sh", ["/bin/sh", "-lc", serve_cmd])

if not os.path.exists("index.html"):
    # A build that produced no entry point should fail loudly, not serve a file listing.
    sys.exit("no index.html in artifact")


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass


# Threading matters: a web game pulls dozens of files, and a serial server would
# serialize the whole load behind one slow transfer.
server = ThreadingHTTPServer(("0.0.0.0", __PORT__), Handler)
server.daemon_threads = True
print("serving on __PORT__", flush=True)
server.serve_forever()
"""


def preview_share_url(token: str) -> str:
    """The link a human opens.

    The ``/api`` prefix is deliberate: it is the only path routed to the API on all three
    deployment targets (the compose nginx proxies ``location /api/``, the k8s Ingress
    sends ``/`` into that same nginx, and the ALB matches ``/api/*``). The API strips it
    back off in StripApiPrefixMiddleware, so the router itself is mounted at ``/p``. A
    bare ``/p/...`` would be served the SPA's index.html on every target."""
    from core.config import get_settings

    base = (get_settings().public_base_url or "").rstrip("/")
    return f"{base}/api/p/{token}/" if base else f"/api/p/{token}/"


def preview_name(repo_id: uuid.UUID) -> str:
    """Deterministic, and therefore the idempotency key: starting a preview twice for one
    repo converges on a single container instead of leaking a second. Also the container's
    DNS alias on socket engines, so it must stay a valid hostname label."""
    return f"pyr-prev-{str(repo_id)[:8]}"


def build_serve_command(*, port: int, serve_cmd: str = "") -> str:
    """The container's whole lifetime as one shell command.

    The program is carried base64-encoded: it contains quotes, newlines and braces that
    would not survive interpolation into ``sh -lc``, and the encoding is alphanumeric so
    it needs no escaping itself.

    ``serve_cmd`` replaces only the *serving* half. Fetching the artifact with its scoped
    token and extracting it under PEP 706's ``filter="data"`` guard stays here whatever the
    recipe says: those are the parts that hold a credential and decide where bytes land,
    and handing them to repo-supplied configuration would buy nothing. An empty
    ``serve_cmd`` is the original static-site server, so every existing preview is
    byte-for-byte unchanged."""
    import base64 as _b64

    program = (
        _SERVE_PROGRAM.replace("__WEBROOT__", _WEBROOT)
        .replace("__PORT__", str(port))
        .replace("__SERVE_CMD__", repr(serve_cmd))
    )
    encoded = _b64.b64encode(program.encode()).decode()
    return f"exec python3 -c \"import base64;exec(base64.b64decode('{encoded}').decode())\""


async def create_preview(
    tenant_id: uuid.UUID,
    *,
    name: str,
    repo_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    artifact_name: str,
    engine_key: str | None,
    image: str,
    ttl_seconds: int,
    created_by_principal_id: uuid.UUID | None = None,
    created_by_label: str = "",
) -> uuid.UUID:
    """Claim the row before anything is started, so a crashed worker leaves a visible
    ``starting`` row rather than an invisible orphan container."""
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(PreviewEnvironmentRow).where(
                PreviewEnvironmentRow.tenant_id == tenant_id,
                PreviewEnvironmentRow.name == name,
            )
        )
        if row is None:
            row = PreviewEnvironmentRow(tenant_id=tenant_id, name=name)
            session.add(row)
        row.repo_id = repo_id
        row.workspace_id = workspace_id
        row.session_id = session_id
        row.artifact_name = artifact_name[:120]
        row.engine_key = engine_key
        row.image = image[:255]
        row.status = "starting"
        row.internal_url = ""
        row.ref = ""
        row.last_error = ""
        row.expires_at = expires_at
        row.created_by_principal_id = created_by_principal_id
        row.created_by_label = (created_by_label or "")[:120]
        await session.flush()
        return row.id


async def mark_running(
    tenant_id: uuid.UUID, preview_id: uuid.UUID, *, ref: str, internal_url: str
) -> None:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(PreviewEnvironmentRow, preview_id)
        if row is None:
            return
        row.ref = ref[:120]
        row.internal_url = internal_url[:255]
        row.status = "running"
        row.last_error = ""


async def mark_failed(tenant_id: uuid.UUID, preview_id: uuid.UUID, error: str) -> None:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(PreviewEnvironmentRow, preview_id)
        if row is None:
            return
        row.status = "failed"
        row.last_error = error[:2000]


async def extend_expiry(
    tenant_id: uuid.UUID, preview_id: uuid.UUID, *, ttl_seconds: int
) -> datetime | None:
    """Push a running preview's deadline to now + ttl. Returns the new deadline.

    Only ever extends: re-sharing a preview that already has six hours left must not
    quietly shorten it to the default."""
    deadline = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    async with tenant_scope(tenant_id) as session:
        row = await session.get(PreviewEnvironmentRow, preview_id)
        if row is None:
            return None
        if row.expires_at is None or row.expires_at < deadline:
            row.expires_at = deadline
        return row.expires_at


async def set_status(tenant_id: uuid.UUID, preview_id: uuid.UUID, status: str) -> bool:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(PreviewEnvironmentRow, preview_id)
        if row is None:
            return False
        row.status = status
        return True


async def get_preview(tenant_id: uuid.UUID, preview_id: uuid.UUID) -> PreviewEnvironmentRow | None:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(PreviewEnvironmentRow, preview_id)
        if row is not None:
            session.expunge(row)
        return row


def is_serveable(row: PreviewEnvironmentRow) -> bool:
    """What the public proxy requires before it will forward a request."""
    if row.status != "running" or not row.internal_url:
        return False
    return not (row.expires_at is not None and row.expires_at <= datetime.now(UTC))


async def list_previews(
    tenant_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
    repo_id: uuid.UUID | None = None,
    include_finished: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    async with tenant_scope(tenant_id) as session:
        query = select(PreviewEnvironmentRow).where(PreviewEnvironmentRow.tenant_id == tenant_id)
        if workspace_id is not None:
            query = query.where(PreviewEnvironmentRow.workspace_id == workspace_id)
        if repo_id is not None:
            query = query.where(PreviewEnvironmentRow.repo_id == repo_id)
        if not include_finished:
            query = query.where(PreviewEnvironmentRow.status.in_(ACTIVE_STATUSES))
        rows = (
            await session.scalars(
                query.order_by(PreviewEnvironmentRow.updated_at.desc()).limit(limit)
            )
        ).all()
        return [
            {
                "id": row.id,
                "name": row.name,
                "status": row.status,
                "engine_key": row.engine_key,
                "image": row.image,
                "repo_id": row.repo_id,
                "workspace_id": row.workspace_id,
                "session_id": row.session_id,
                "artifact_name": row.artifact_name,
                "created_by_label": row.created_by_label,
                "last_error": row.last_error,
                "expires_at": row.expires_at,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in rows
        ]


async def due_for_reaping() -> list[tuple[uuid.UUID, uuid.UUID, str, str | None]]:
    """(tenant_id, preview_id, name, engine_key) for every expired live preview.

    Cross-tenant work without breaking tenancy: enumerate tenants on the ``tenant`` table
    (one of the explicitly unscoped tables) and then open a normal ``tenant_scope`` per
    tenant -- the shape ``core.exec_envs`` already uses for teardown sweeps."""
    from core.tenancy.models import Tenant
    from core.tenancy.scope import unscoped_session

    async with unscoped_session() as session:
        tenant_ids = list(await session.scalars(select(Tenant.id)))

    now = datetime.now(UTC)
    due: list[tuple[uuid.UUID, uuid.UUID, str, str | None]] = []
    for tenant_id in tenant_ids:
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.scalars(
                    select(PreviewEnvironmentRow).where(
                        PreviewEnvironmentRow.tenant_id == tenant_id,
                        PreviewEnvironmentRow.status.in_(ACTIVE_STATUSES),
                        PreviewEnvironmentRow.expires_at.is_not(None),
                        PreviewEnvironmentRow.expires_at <= now,
                    )
                )
            ).all()
            due.extend((tenant_id, r.id, r.name, r.engine_key) for r in rows)
    return due
