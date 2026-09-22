"""Getting the retrieval models onto the box, and knowing whether they are there.

Both installers used to block on a multi-gigabyte download before handing the deployment
over: a fresh install stalled for minutes on something that has nothing to do with whether
the install worked, and an air-gapped box could not finish at all. The download is a
*warm-up* of the same lazy path the providers take anyway, so it does not have to happen
then, and it does not have to happen there.

Three ways a model gets here, and this module knows about all of them:

* **Lazily**, the first time something embeds -- always true, and why nothing here is
  required for correctness. It just makes that first turn slow.
* **On demand**, from the admin console, as a worker job. Same fetch, visible, and
  restartable without touching the installer.
* **From an upload**, for a box with no route to Hugging Face: the operator brings the
  cache directory as a tarball.

The cache is a shared volume (``HF_HOME``) mounted into both the api and the worker, which
is what lets the worker do the fetching and the api see the result.
"""

from __future__ import annotations

import importlib
import os
import pathlib
import shutil
import tarfile
from dataclasses import dataclass
from typing import IO, Any

# huggingface_hub turns "BAAI/bge-m3" into "models--BAAI--bge-m3" on disk.
_REPO_DIR_PREFIX = "models--"


def cache_root() -> pathlib.Path:
    """Where the HF cache lives, honouring the same env the libraries read."""
    home = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
    return pathlib.Path(home) / "hub"


def _repo_dir(model_ref: str) -> pathlib.Path:
    repo = model_ref.split("/", 1)[-1] if model_ref.startswith("local/") else model_ref
    return cache_root() / (_REPO_DIR_PREFIX + repo.replace("/", "--"))


def _dir_size_mb(path: pathlib.Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:  # a blob being written underneath us is not an error to report
            continue
    return total // (1024 * 1024)


@dataclass(frozen=True)
class ModelPresence:
    model: str
    present: bool
    size_mb: int

    def as_dict(self) -> dict[str, Any]:
        return {"model": self.model, "present": self.present, "size_mb": self.size_mb}


def presence(model_ref: str) -> ModelPresence:
    """Is this model on disk, and how big is it?

    "Present" means the repo directory holds at least one blob. A directory containing
    only refs/ metadata is what a half-finished download looks like, and calling that
    present would tell an operator everything is fine while the next turn stalls."""
    directory = _repo_dir(model_ref)
    blobs = directory / "blobs"
    has_blob = blobs.is_dir() and any(p.is_file() for p in blobs.iterdir())
    return ModelPresence(model=model_ref, present=has_blob, size_mb=_dir_size_mb(directory))


async def cache_status() -> dict[str, Any]:
    """What the admin console shows: each configured model, and where the cache is."""
    from core.deployment_settings import get_retrieval_models

    effective = await get_retrieval_models()
    models = [presence(str(effective["embedding_model"]))]
    if effective.get("reranker_enabled"):
        models.append(presence(str(effective["reranker_model"])))
    return {
        "cache_path": str(cache_root()),
        "offline": os.environ.get("HF_HUB_OFFLINE", "") in ("1", "true", "True"),
        "models": [m.as_dict() for m in models],
        "total_mb": sum(m.size_mb for m in models),
    }


def fetch_models(embedding_model: str, reranker_model: str | None) -> dict[str, Any]:
    """Pull the models from Hugging Face into the shared cache.

    Deliberately the same constructor call the providers make, rather than a bespoke
    download: whatever that pulls is by definition what the runtime needs, so this cannot
    fetch a subtly different set of files. Blocking and slow -- the caller is a worker job.
    """
    # The deployment runs offline so a turn never reaches the network mid-generation, and
    # an explicit fetch is the one moment it should.
    #
    # Setting the environment variables is not enough and looks like it is. huggingface_hub
    # reads HF_HUB_OFFLINE once, at import, into a module constant; by the time this runs
    # the worker has long since imported it through the embedding provider, so flipping
    # os.environ changes a string nobody reads again. The download then fails with
    # "couldn't connect to huggingface.co, and couldn't find them in the cached files" on a
    # machine whose network is fine -- which is exactly the wrong message, and cost an
    # evening of looking at DNS.
    #
    # So flip the constants the libraries actually consult, and put them back. Both are
    # set: the environment for anything imported after this point, the constants for
    # everything already holding a copy.
    offline_vars = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    previous_env = {name: os.environ.get(name) for name in offline_vars}
    for name in offline_vars:
        os.environ[name] = "0"

    patched: list[tuple[Any, str, Any]] = []

    def _force_online(module_name: str, attr: str) -> None:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 -- a library that is not installed is not a problem
            return
        if hasattr(module, attr):
            patched.append((module, attr, getattr(module, attr)))
            setattr(module, attr, False)

    _force_online("huggingface_hub.constants", "HF_HUB_OFFLINE")
    _force_online("transformers.utils.hub", "_is_offline_mode")

    try:
        from sentence_transformers import CrossEncoder, SentenceTransformer

        SentenceTransformer(embedding_model.split("/", 1)[-1])
        if reranker_model:
            CrossEncoder(reranker_model.split("/", 1)[-1])
    finally:
        for module, attr, was in patched:
            setattr(module, attr, was)
        for name, was in previous_env.items():
            if was is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = was

    return {
        "embedding": presence(embedding_model).as_dict(),
        "reranker": presence(reranker_model).as_dict() if reranker_model else None,
    }


class CacheUploadError(ValueError):
    """An upload that is not a usable HF cache. Always says what was wrong."""


def install_from_tarball(stream: IO[bytes], *, max_bytes: int) -> dict[str, Any]:
    """Unpack an operator-supplied HF cache tarball into the shared volume.

    For a deployment with no route to Hugging Face: fetch the cache on a machine that has
    one, tar it, bring it here. Extraction refuses absolute paths, traversal and special
    files (``filter="data"``, PEP 706) -- this writes into a volume both services read, so
    a malicious archive would otherwise be writing anywhere the process can reach.

    Accepts an archive rooted either at the cache directory itself or at its ``hub/``
    parent, because both are things a person reasonably produces with ``tar``.
    """
    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    staging = root.parent / ".upload-staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        # The ceiling counts EXTRACTED bytes, not bytes read off the wire. A decompression
        # bomb is small compressed and enormous unpacked, so a limit on the input is no
        # limit at all -- which a test caught only because it tried to bomb a 1KB ceiling
        # with 200KB of compressible zeroes and sailed through.
        extracted = 0
        with tarfile.open(fileobj=stream, mode="r|*") as tar:
            for member in tar:
                extracted += max(member.size, 0)
                if extracted > max_bytes:
                    raise CacheUploadError(
                        f"archive unpacks to more than the {max_bytes // (1024 * 1024)}MB ceiling"
                    )
                tar.extract(member, staging, filter="data")

        found = [p for p in staging.rglob(f"{_REPO_DIR_PREFIX}*") if p.is_dir()]
        if not found:
            raise CacheUploadError(
                "no model directories in the archive -- expected at least one "
                f"'{_REPO_DIR_PREFIX}<org>--<name>' directory, which is how the Hugging "
                "Face cache names them. Tar the 'hub' directory of an HF cache."
            )
        installed = []
        for directory in found:
            target = root / directory.name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.move(str(directory), str(target))
            installed.append(directory.name)
        return {"installed": sorted(installed), "cache_path": str(root)}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
