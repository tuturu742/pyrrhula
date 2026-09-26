"""The `.pyr` bundle format (G4.5, req 23/24): a ZIP with a `manifest.json`,
JSON for objects, JSONL for logs, Markdown for entry bodies, and a sha256 per file.

Format decisions, all from and all load-bearing:

* **Entry bodies are separate `.md` files.** A bundle is then diffable in git, which is a
  real workflow for rules-authors and costs nothing.
* **`pyr_format` is the *format* version, not the app version.** Compatibility is
  `pyr_format`-major; export always writes current, import supports N and N-1 through an
  upcast chain. An app version is recorded too, but only as provenance -- nothing
  branches on it, because "which app wrote this" is a support question and "what shape is
  this" is a compatibility question, and conflating them is how format handling rots.
* **Integrity is per file, plus the resolution hash chain on top.** The per-file hashes
  catch a tampered bundle; the chain (verified at import, G4.6) catches a tampered *resolution
  history* specifically, so an archived session can prove nobody edited the rolls.
* **Omissions are stubs, not silence.** Everything the exporter's visibility excluded lands
  in `manifest.redactions[]` as `{type, id, reason}`. A recipient can always tell the
  difference between "this workspace had no secrets" and "you weren't shown them" -- and
  that difference is exactly what makes a sanitised bundle honest rather than merely quiet.

This module is deliberately free of any policy: it writes and reads bytes. *What* goes in
is ``export.py``'s decision, resolved through the one visibility resolver.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

PYR_FORMAT = 1
MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class Redaction:
    """One thing the bundle does not contain, and why. ``type`` is the object kind
    (``secret``, ``knowledge_entry``, ``entity``, ...), ``id`` its identifier."""

    type: str
    id: str
    reason: str

    def to_json(self) -> dict[str, str]:
        return {"type": self.type, "id": self.id, "reason": self.reason}


@dataclass
class BundleWriter:
    """Accumulates files, then seals them into a ZIP with a manifest. Deterministic: files
    are written in sorted path order with a fixed timestamp, so exporting the same content
    twice produces byte-identical archives. That is not tidiness -- it is what lets a
    caller diff two exports, or hash a bundle and mean something by it."""

    tenant_ref: str
    app_version: str
    # The source tenant's pinned workflow, so an importer can load the pack the content
    # depends on. Defaulted so every existing construction site stays valid.
    workflow_key: str = ""
    exported_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    files: dict[str, bytes] = field(default_factory=dict)
    redactions: list[Redaction] = field(default_factory=list)

    def add_bytes(self, path: str, data: bytes) -> None:
        if path == MANIFEST_NAME:
            raise ValueError(f"{MANIFEST_NAME} is written by seal(), not added directly")
        if path in self.files:
            raise ValueError(f"duplicate bundle path {path!r}")
        self.files[path] = data

    def add_json(self, path: str, payload: Any) -> None:
        self.add_bytes(path, _canonical_json(payload).encode())

    def add_jsonl(self, path: str, records: list[dict[str, Any]]) -> None:
        body = "".join(f"{_canonical_json(r)}\n" for r in records)
        self.add_bytes(path, body.encode())

    def add_text(self, path: str, text: str) -> None:
        self.add_bytes(path, text.encode())

    def redact(self, type_: str, id_: str, reason: str) -> None:
        self.redactions.append(Redaction(type=type_, id=id_, reason=reason))

    def build_manifest(self) -> dict[str, Any]:
        paths = sorted(self.files)
        return {
            "pyr_format": PYR_FORMAT,
            "app_version": self.app_version,
            "exported_at": self.exported_at.isoformat(),
            "tenant_ref": self.tenant_ref,
            # The workflow this content was authored under. Behaviour profiles reference
            # axes by pack ("rpg_v1"), and those axes only exist in a tenant once its
            # workflow's pack has been loaded -- so a bundle that does not say which
            # workflow it needs imports its personas with their sliders silently dropped.
            # Empty when the source tenant had no workflow pinned. Additive: older
            # bundles simply lack the key.
            "workflow_key": self.workflow_key,
            "contents": paths,
            "integrity": {p: hashlib.sha256(self.files[p]).hexdigest() for p in paths},
            "redactions": [r.to_json() for r in self.redactions],
        }

    def seal(self) -> bytes:
        manifest = _canonical_json(self.build_manifest()).encode()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(_fixed_info(MANIFEST_NAME), manifest)
            for path in sorted(self.files):
                archive.writestr(_fixed_info(path), self.files[path])
        return buffer.getvalue()


class BundleIntegrityError(Exception):
    """A file's sha256 doesn't match `manifest.integrity`, or a listed file is missing, or
    an unlisted file is present. All three are the same failure from a recipient's point of
    view: the bundle is not what its manifest says it is."""


@dataclass(frozen=True)
class BundleReader:
    manifest: dict[str, Any]
    files: dict[str, bytes]

    @property
    def pyr_format(self) -> int:
        return int(self.manifest["pyr_format"])

    @property
    def redactions(self) -> list[dict[str, str]]:
        return [dict(r) for r in self.manifest.get("redactions", [])]

    def read_json(self, path: str) -> Any:
        return json.loads(self.files[path].decode())

    def read_jsonl(self, path: str) -> list[Any]:
        text = self.files[path].decode()
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def read_text(self, path: str) -> str:
        return self.files[path].decode()

    def paths_under(self, prefix: str) -> list[str]:
        return sorted(p for p in self.files if p.startswith(prefix))


def open_bundle(data: bytes) -> BundleReader:
    """Reads a bundle *without* verifying it. Separate from ``verify_bundle`` on purpose:
    the import needs to read the manifest (to learn `pyr_format`, to report *where* a
    break is) even when verification has already failed, and a reader that refused to open
    a damaged bundle could only ever say "it's broken" with no detail."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [n for n in archive.namelist() if not n.endswith("/")]
        if MANIFEST_NAME not in names:
            raise BundleIntegrityError(f"bundle has no {MANIFEST_NAME}")
        manifest = json.loads(archive.read(MANIFEST_NAME).decode())
        files = {n: archive.read(n) for n in names if n != MANIFEST_NAME}
    return BundleReader(manifest=manifest, files=files)


def verify_bundle(reader: BundleReader) -> list[str]:
    """Returns the paths that fail verification -- mismatched hash, listed-but-absent, or
    present-but-unlisted. An empty list means the bundle is exactly what its manifest
    claims. Returning the list rather than raising lets a caller report every problem at
    once instead of one per attempt."""
    integrity = reader.manifest.get("integrity", {})
    listed = set(reader.manifest.get("contents", []))
    present = set(reader.files)

    broken = sorted((listed - present) | (present - listed))
    for path in sorted(listed & present):
        expected = integrity.get(path)
        if expected != hashlib.sha256(reader.files[path]).hexdigest():
            broken.append(path)
    return sorted(set(broken))


def _canonical_json(payload: Any) -> str:
    """Sorted keys, no incidental whitespace: a hash over JSON is only meaningful if the
    JSON is produced one way.

    Deliberately NOT ``core.audit.canonical.canonical_json``, despite the shared purpose.
    That one rejects floats outright, because a hash chain that drifts with float
    repr is worse than no chain -- but a bundle carries flow definitions, and a phase
    budget is literally ``{"rules": 0.2, "lore": 0.55}``. Routing bundles through the
    strict version would refuse to export any workspace with a budget ratio. The looser
    rule here is the price of hashing author-supplied content rather than rows we
    control; do not "consolidate" these two."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _fixed_info(path: str) -> zipfile.ZipInfo:
    """A fixed 1980-01-01 timestamp (ZIP's own epoch). Real mtimes would make two exports
    of identical content differ byte-for-byte, which defeats diffing them."""
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info
