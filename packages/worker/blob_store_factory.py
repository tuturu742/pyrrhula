"""The worker's composition root for ``BlobStore`` selection — mirrors
``api.blob_store_factory`` . Same local-filesystem root as the api process (shared
volume in the compose stack) so a blob the api wrote is readable here.
"""

from __future__ import annotations

from adapters.blob.local.filesystem import LocalFilesystemBlobStore
from core.config import get_settings
from core.ports.blob_store import BlobStore

_blob_store: BlobStore | None = None


def get_blob_store() -> BlobStore:
    # A configured bucket swaps the whole store behind the port -- call sites
    # never learn which one they got.
    from adapters.blob.s3.provider import s3_store_from_settings

    s3 = s3_store_from_settings()
    if s3 is not None:
        return s3
    global _blob_store
    if _blob_store is None:
        _blob_store = LocalFilesystemBlobStore(get_settings().blob_store_root)
    return _blob_store
