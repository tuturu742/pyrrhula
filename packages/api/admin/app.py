"""DEPRECATED standalone admin app (separate port, ``docker/entrypoint.sh admin``).

Platform administration now lives on the MAIN api under ``/admin`` (see
``api.routes.admin``), reached through the web UI by logging in with organization
``admin``. This app remains only as a compatibility shim for deployments still
exposing the dedicated port: it mounts the very same router (so the legacy
``PYRRHULA_ADMIN_TOKEN`` bearer keeps working here too) plus the old static console.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from api.routes.admin import router as admin_router

_INDEX_HTML = (Path(__file__).parent / "static" / "index.html").read_text()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Kept for deployments that boot the admin app before (or instead of) the main
    # api: fresh databases still get the baked default workflow plugin. Idempotent;
    # the main api's lifespan does the same.
    try:
        from core.plugins.service import ensure_default_synced

        await ensure_default_synced()
    except Exception as exc:  # noqa: BLE001
        structlog.get_logger().warning("plugins.default_sync_failed", error=str(exc)[:300])
    yield


app = FastAPI(title="Pyrrhula Admin", docs_url="/api-docs", lifespan=_lifespan)
app.include_router(admin_router)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _INDEX_HTML


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
