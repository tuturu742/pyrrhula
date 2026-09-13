"""The api composition root for ``BlobStore`` selection (A1.2) — v1 is always the local
filesystem adapter, rooted at ``Settings.blob_store_root``. A single process-wide instance
(the adapter itself is just a resolved root path, no connection to manage) rather than
constructing one per request.
"""

from __future__ import annotations

from adapters.blob.local.filesystem import LocalFilesystemBlobStore
from core.config import get_settings
from core.ports.blob_store import BlobStore

_blob_store: BlobStore | None = None


def get_blob_store() -> BlobStore:
    # A configured bucket swaps the whole store behind the port -- call sites
    # never learn which one they got (§13.8).
    from adapters.blob.s3.provider import s3_store_from_settings

    s3 = s3_store_from_settings()
    if s3 is not None:
        return s3
    global _blob_store
    if _blob_store is None:
        _blob_store = LocalFilesystemBlobStore(get_settings().blob_store_root)
    return _blob_store
