"""BlobStore port. v1 is local filesystem; S3-compatible adapter is a swap behind this
port for the SaaS deployment mode without touching call sites."""

from __future__ import annotations

from typing import Protocol


class BlobNotFoundError(Exception):
    pass


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    async def exists(self, key: str) -> bool: ...
