"""The api composition root for ``BlobStore`` selection.

The local filesystem adapter rooted at ``Settings.blob_store_root``, unless
``blob_s3_bucket`` is set -- then the S3-compatible one. Choosing between them *is* what a
composition root is for, so the branch lives here and nowhere else; the docstring used to
say "always the local filesystem adapter", which its own body had already stopped being
true of. A single process-wide instance (the local adapter is just a resolved root path,
no connection to manage) rather than constructing one per request.
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
