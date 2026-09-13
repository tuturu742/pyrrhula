import pytest

from adapters.blob.local.filesystem import LocalFilesystemBlobStore
from core.ports.blob_store import BlobNotFoundError, BlobStore


async def test_put_get_exists_delete(tmp_path) -> None:
    store: BlobStore = LocalFilesystemBlobStore(tmp_path)

    assert await store.exists("a/b.txt") is False

    await store.put("a/b.txt", b"hello", content_type="text/plain")
    assert await store.exists("a/b.txt") is True
    assert await store.get("a/b.txt") == b"hello"

    await store.delete("a/b.txt")
    assert await store.exists("a/b.txt") is False


async def test_get_missing_raises(tmp_path) -> None:
    store: BlobStore = LocalFilesystemBlobStore(tmp_path)
    with pytest.raises(BlobNotFoundError):
        await store.get("does/not/exist.txt")


async def test_rejects_path_escape(tmp_path) -> None:
    store = LocalFilesystemBlobStore(tmp_path)
    with pytest.raises(ValueError, match="invalid blob key|escapes store root"):
        await store.put("../escape.txt", b"x")
