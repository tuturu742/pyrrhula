"""`pyr_format` upcast chain.

Compatibility is `pyr_format`-major: export always writes current, import supports the
current major and the one before it, and gets from one to the other by applying pure
transforms in sequence -- `0 -> 1`, `1 -> 2`, ... -- exactly the way DB migrations work,
and for the same reason. A single "read any old shape" branch tree is where format
handling rots; a chain of small, individually-testable steps is where it doesn't.

**Transforms are pure.** Each takes a `{path: bytes}` map plus the manifest and returns
new ones. No database, no clock, no IO: a transform is testable by calling it, and a
bundle can be upcast without touching the tenant it is destined for.

**Refuse a future major loudly.** A bundle written by a newer version is not something to
guess at -- `UnsupportedFormatError` names the version it needs, because "import failed"
with no version in it sends a user to a forum instead of to an upgrade.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from core.portability.bundle import PYR_FORMAT

UpcastStep = Callable[[dict[str, bytes], dict[str, Any]], tuple[dict[str, bytes], dict[str, Any]]]


class UnsupportedFormatError(Exception):
    """The bundle's `pyr_format` is newer than this build understands, or older than the
    chain reaches."""


def _upcast_0_to_1(
    files: dict[str, bytes], manifest: dict[str, Any]
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Format 0 -> 1: split each source's aggregated ``chunk_texts.json`` map into one
    ``chunks/<chunk_id>.json`` file per chunk.

    Format 0 kept every chunk of a source in a single JSON object keyed by chunk id.
    Format 1 gives each chunk its own file for the same reason gives for entry
    bodies as `.md`: a bundle should diff at the granularity people actually edit at, and
    a one-line change inside a thousand-chunk map diffs as a change to the whole map.

    Nothing is invented and nothing is dropped -- chunk ids, texts, and metadata carry
    across unchanged, which is why a manifest recorded against a format-0 bundle still
    cites chunks that exist after the upcast, and why an upcast bundle still replays.
    """
    upcast = {p: b for p, b in files.items() if not p.endswith("/chunk_texts.json")}
    for path in sorted(files):
        if not path.endswith("/chunk_texts.json"):
            continue
        prefix = path.removesuffix("/chunk_texts.json")
        chunk_map = json.loads(files[path].decode())
        for chunk_id, payload in chunk_map.items():
            record = {"id": chunk_id, **payload}
            upcast[f"{prefix}/chunks/{chunk_id}.json"] = json.dumps(
                record, sort_keys=True, separators=(",", ":")
            ).encode()

    new_manifest = dict(manifest)
    new_manifest["pyr_format"] = 1
    new_manifest["contents"] = sorted(upcast)
    new_manifest["integrity"] = {p: hashlib.sha256(upcast[p]).hexdigest() for p in sorted(upcast)}
    return upcast, new_manifest


# Keyed by the format the step reads. Add a step here, never a branch in `import_.py`.
STEPS: dict[int, UpcastStep] = {0: _upcast_0_to_1}

OLDEST_SUPPORTED_FORMAT = min(STEPS) if STEPS else PYR_FORMAT


def upcast_to_current(
    files: dict[str, bytes], manifest: dict[str, Any]
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Applies every step from the bundle's format up to the current one. A bundle already
    at the current format passes through untouched -- no step runs, and its bytes are the
    ones that get imported, so the common path costs nothing."""
    version = int(manifest.get("pyr_format", PYR_FORMAT))
    if version > PYR_FORMAT:
        raise UnsupportedFormatError(
            f"bundle is pyr_format {version}; this build writes and reads up to "
            f"{PYR_FORMAT}. Upgrade Pyrrhula to import it."
        )
    if version < OLDEST_SUPPORTED_FORMAT:
        raise UnsupportedFormatError(
            f"bundle is pyr_format {version}; the oldest format this build can upcast "
            f"from is {OLDEST_SUPPORTED_FORMAT}."
        )

    while version < PYR_FORMAT:
        step = STEPS.get(version)
        if step is None:
            raise UnsupportedFormatError(
                f"no upcast step registered for pyr_format {version} -> {version + 1}"
            )
        files, manifest = step(files, manifest)
        version = int(manifest["pyr_format"])
    return files, manifest
