"""v1 BlobStore: local filesystem, rooted at a configurable base directory. Fine for
self-host; the SaaS deployment mode swaps in an S3-compatible adapter (packages/adapters/
blob/s3/, not yet implemented) behind the same port."""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.ports.blob_store import BlobNotFoundError


class LocalFilesystemBlobStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        if not key or key.startswith("/") or ".." in Path(key).parts:
            raise ValueError(f"invalid blob key: {key!r}")
        path = (self._root / key).resolve()
        if self._root not in path.parents and path != self._root:
            raise ValueError(f"blob key escapes store root: {key!r}")
        return path

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        path = self._resolve(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(_write)

    async def get(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.is_file():
            raise BlobNotFoundError(key)
        return await asyncio.to_thread(path.read_bytes)

    async def delete(self, key: str) -> None:
        path = self._resolve(key)
        await asyncio.to_thread(path.unlink, True)  # missing_ok=True

    async def exists(self, key: str) -> bool:
        path = self._resolve(key)
        return await asyncio.to_thread(path.is_file)
