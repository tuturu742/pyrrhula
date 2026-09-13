"""S3-compatible BlobStore adapter (§13.8): the same port the local filesystem store
implements, backed by S3/MinIO/R2/Spaces via botocore (already a dependency through the
AWS exec engine).

Configuration (``core.config.Settings``):
- ``PYRRHULA_BLOB_S3_BUCKET``    -- set = this adapter is selected by the factories
- ``PYRRHULA_BLOB_S3_ENDPOINT``  -- MinIO/R2/Spaces base URL; empty = real AWS S3
- ``PYRRHULA_BLOB_S3_REGION``    -- default ``us-east-1``
- ``PYRRHULA_BLOB_S3_PREFIX``    -- optional key prefix so one bucket can host several
  deployments without collisions

Credentials come from the ambient AWS chain (instance role, IRSA, env vars) -- never
from Pyrrhula config, for the same reason ``credential_ref`` never holds a key.

Boto is synchronous, so each call runs in a worker thread: the event loop is never
blocked on network I/O, and the adapter stays a thin translation of four operations.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any

from core.config import get_settings
from core.ports.blob_store import BlobNotFoundError


class S3BlobStore:
    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        prefix: str = "",
        client: Any | None = None,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        if client is not None:
            self._client = client
        else:
            import boto3  # imported lazily: only S3 deployments need it

            self._client = boto3.client("s3", endpoint_url=endpoint_url or None, region_name=region)

    def _key(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    async def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(fn, *args, **kwargs)
        )

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        await self._run(
            self._client.put_object, Bucket=self._bucket, Key=self._key(key), Body=data, **extra
        )

    async def get(self, key: str) -> bytes:
        try:
            response = await self._run(
                self._client.get_object, Bucket=self._bucket, Key=self._key(key)
            )
        except Exception as exc:  # noqa: BLE001 -- botocore's ClientError taxonomy
            if "NoSuchKey" in str(exc) or "404" in str(exc):
                raise BlobNotFoundError(key) from exc
            raise
        body: bytes = await self._run(response["Body"].read)
        return body

    async def delete(self, key: str) -> None:
        await self._run(self._client.delete_object, Bucket=self._bucket, Key=self._key(key))

    async def exists(self, key: str) -> bool:
        try:
            await self._run(self._client.head_object, Bucket=self._bucket, Key=self._key(key))
        except Exception:  # noqa: BLE001 -- absent or inaccessible: both "no"
            return False
        return True


def s3_store_from_settings() -> S3BlobStore | None:
    """The factory hook: a configured bucket selects S3, otherwise callers keep the
    local filesystem store."""
    settings = get_settings()
    bucket = getattr(settings, "blob_s3_bucket", "")
    if not bucket:
        return None
    return S3BlobStore(
        bucket,
        endpoint_url=getattr(settings, "blob_s3_endpoint", "") or None,
        region=getattr(settings, "blob_s3_region", "us-east-1"),
        prefix=getattr(settings, "blob_s3_prefix", ""),
    )
