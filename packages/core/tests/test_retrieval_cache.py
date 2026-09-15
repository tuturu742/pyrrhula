"""Getting the retrieval models onto the box without the installer doing it."""

from __future__ import annotations

import io
import pathlib
import tarfile

import pytest

from core.retrieval_cache import CacheUploadError, install_from_tarball, presence


@pytest.fixture
def cache(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    return tmp_path / "hub"


def _archive(entries: dict[str, bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    buf.seek(0)
    return buf


def test_a_model_with_no_blobs_is_not_present(cache: pathlib.Path) -> None:
    """A directory holding only refs metadata is what a half-finished download looks
    like. Calling that present tells an operator everything is fine while the next turn
    stalls on a cold fetch."""
    (cache / "models--BAAI--bge-m3" / "refs").mkdir(parents=True)
    assert presence("local/BAAI/bge-m3").present is False

    blobs = cache / "models--BAAI--bge-m3" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "abc123").write_bytes(b"x" * 2048)
    assert presence("local/BAAI/bge-m3").present is True


def test_an_uploaded_cache_lands_where_the_providers_look(cache: pathlib.Path) -> None:
    """The air-gapped path: a box with no route to huggingface.co cannot download, and
    waiting for one is not a deployment story."""
    result = install_from_tarball(
        _archive({"hub/models--BAAI--bge-m3/blobs/abc": b"weights"}), max_bytes=10_000_000
    )

    assert result["installed"] == ["models--BAAI--bge-m3"]
    assert presence("local/BAAI/bge-m3").present is True


def test_an_archive_without_model_directories_is_refused(cache: pathlib.Path) -> None:
    """Saying what was expected beats installing nothing and reporting success."""
    with pytest.raises(CacheUploadError, match="no model directories"):
        install_from_tarball(_archive({"notes.txt": b"hello"}), max_bytes=10_000_000)


def test_a_traversing_archive_cannot_write_outside_the_cache(
    cache: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """This unpacks into a volume both services read from, so an archive that could
    write anywhere the process can reach is the whole risk of accepting one at all."""
    victim = tmp_path / "escaped.txt"
    with pytest.raises(Exception):  # noqa: B017 -- tarfile's own filter refusal
        install_from_tarball(_archive({"../../escaped.txt": b"owned"}), max_bytes=10_000_000)
    assert not victim.exists()


def test_an_oversized_upload_is_cut_off(cache: pathlib.Path) -> None:
    """A decompression bomb must not fill the volume the running deployment reads from."""
    with pytest.raises(CacheUploadError, match="ceiling"):
        install_from_tarball(
            _archive({"hub/models--x--y/blobs/big": b"z" * 200_000}), max_bytes=1024
        )
